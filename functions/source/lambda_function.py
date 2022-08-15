import os
import requests
import meraki
import boto3
import botocore
import logging
import json
import sys
import threading


from botocore.exceptions import ClientError

logging.basicConfig(stream = sys.stdout)
logger = logging.getLogger()
logger.setLevel(logging.INFO)

ORG_ID = os.environ['MERAKI_ORG_ID']
TGW_RT_ID = os.environ['TGW_RT_ID']
TGW_ATTACH_ID = os.environ['TGW_ATTACH_ID']
RT_ID = os.environ['RT_ID']
VMX1_TAG = os.environ['vMX1Tag']
VMX2_TAG = os.environ['vMX2Tag']

def get_meraki_key():
    secret_name = 'MerakiAPIKey'
    region = os.environ['AWS_REGION']
    session = boto3.session.Session()
    client = session.client(
        service_name='secretsmanager',
        region_name=region,
    )
    try:
        get_secret_value_response = client.get_secret_value(
            SecretId=secret_name
        )
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceNotFoundException':
            logger.info('The requested secret ' + secret_name + ' was not found')
        elif e.response['Error']['Code'] == 'InvalidRequestException':
            logger.info('The request was invalid due to {}:'.format(e))
        elif e.response['Error']['Code'] == 'InvalidParameterException':
            logger.info('The request had invalid params: {}'.format(e))
    else:
        # Secrets Manager decrypts the secret value using the associated KMS CMK
        # Depending on whether the secret was a string or binary, only one of these fields will be populated
        if 'SecretString' in get_secret_value_response:
            text_secret_data = json.loads(get_secret_value_response['SecretString'])
            merakiapikey = text_secret_data['MerakiApiKey']
            return merakiapikey
        else:
            binary_secret_data = get_secret_value_response['SecretBinary']
            return binary_secret_data
    
def get_all_vpn_routes(dashboard, org_id, vmx1_id, vmx2_id):
    org_vpn_status = dashboard.appliance.getOrganizationApplianceVpnStatuses(
    org_id, total_pages='all'
    )
    vpn_routes_vmx1 = []
    vpn_routes_vmx2 = []
    for networks in org_vpn_status:
        if networks['vpnMode'] == 'spoke': 
            for peers in networks['merakiVpnPeers']:
                if peers['networkId'] == vmx1_id or peers['networkId'] == vmx2_id:
                    vpn_status = dashboard.appliance.getNetworkApplianceVpnSiteToSiteVpn(networks['networkId'])
                    for i in vpn_status['hubs']:
                        if i['hubId'] == vmx1_id:
                            for subnets in networks['exportedSubnets']:
                                logger.info('Meraki Dashboard: Found routes for vMX1 hub')
                                vpn_routes_vmx1.append(subnets.get('subnet'))
                                logger.info(vpn_routes_vmx1)
                            break
                        elif i['hubId'] == vmx2_id:
                            for subnets in networks['exportedSubnets']:
                                logger.info('Meraki Dashboard: Found routes for vMX2 hub')
                                vpn_routes_vmx2.append(subnets.get('subnet'))
                                logger.info(vpn_routes_vmx2)
                            break
                else:
                    logger.info('Meraki Dashboard: No routes found for vMX Hubs')
                    pass 
    return vpn_routes_vmx1, vpn_routes_vmx2

def get_meraki_tagged_networks(dashboard, org_id, vmx_tag):
    # executing API call to obtain all Meraki networks in the organization
    organization_networks_response = dashboard.organizations.getOrganizationNetworks(
        org_id, total_pages='all'
    )
    vmx_network = [x for x in organization_networks_response if str(vmx_tag) in str(x['tags'])[1:-1]]

    return vmx_network[0]['id']

def check_vmx_status(dashboard, org_id, vmx_id, ec2_vmx_id):
    region = os.environ['AWS_REGION']
    ec2 = boto3.client('ec2', region_name=region) 
    org_device_status = dashboard.organizations.getOrganizationDevicesStatuses(
        org_id, total_pages='all'
    )
    logger.info('Checking vMX status for meraki org id {0} and ec2 instance id {1}'.format(vmx_id, ec2_vmx_id))
    meraki_vmx_status = [x for x in org_device_status if str(vmx_id) in str(x['networkId'])][0]['status']
    ec2_vmx_status = ec2.describe_instance_status(InstanceIds=[ec2_vmx_id], IncludeAllInstances=True)
    if meraki_vmx_status == 'online' and ec2_vmx_status['InstanceStatuses'][0]['InstanceState']['Name'] == 'running':
        vmx_status = 'online'
    else:
        vmx_status ='offline'

    return vmx_status
            
def update_tgw_rt(vpn_routes, tgw_rt_id, tgw_attach_id):
    region = os.environ['AWS_REGION']
    ec2 = boto3.client('ec2', region_name=region)
    uniq_vpn_routes = list(set(vpn_routes))
    logger.info("EC2 TGW Route Update {0}".format(uniq_vpn_routes))
    #Checking if the route already exsists, if so skip updating the TGW route table
    for route in uniq_vpn_routes:
        exsisting_route = ec2.search_transit_gateway_routes(
            TransitGatewayRouteTableId= tgw_rt_id,
            Filters=[
                { 'Name': 'route-search.exact-match',
                  'Values': [route]

            }]
        )
        if bool(exsisting_route['Routes']):
            logger.info("Transit Gateway RT: No update, route {0} exsists, skipping update".format(route))
            pass
        else:
            logger.info("Transit Gateway RT: New route, adding route {0}".format(route))
            ec2.create_transit_gateway_route(
            DestinationCidrBlock= route,
            TransitGatewayRouteTableId=tgw_rt_id,
            TransitGatewayAttachmentId=tgw_attach_id
           )

def update_vpc_rt(vpn_routes, vmx_id, rt_id):
    region = os.environ['AWS_REGION']
    ec2 = boto3.client('ec2', region_name=region)
    uniq_vpn_routes = list(set(vpn_routes))
    #Checking exsisting routes in the VPC table
    raw_exsisting_vpc_rts = ec2.describe_route_tables(Filters = [{"Name": "route-table-id", "Values": [rt_id]}])['RouteTables'][0]['Routes']
    exsisting_routes = []
    for routes in raw_exsisting_vpc_rts:
        if 'InstanceId' in routes and routes['InstanceId'] == vmx_id:
            exsisting_routes.append(routes['DestinationCidrBlock'])
        else:
            logger.info('VPC RT: No matching routes found')
    #Compare exsisting routes with new routes
    update_routes = [x for x in exsisting_routes + uniq_vpn_routes if x not in exsisting_routes]
    if update_routes:
        logger.info('VPC RT: New routes for update {0}'.format(update_routes))
        for routes in update_routes:
            try:
                ec2.create_route(
                DestinationCidrBlock=routes,
                InstanceId=vmx_id,
                RouteTableId=rt_id
              )
            except botocore.exceptions.ClientError as error:
                if error.response['Error']['Code'] == 'RouteAlreadyExists':
                    ec2.replace_route(
                    DestinationCidrBlock=routes,
                    InstanceId=vmx_id,
                    RouteTableId=rt_id
                )
                else:
                    logger.info('VPC RT: Boto exception, adding routes to vpc table failed due to {0}'.format(error.response['Error']['Code'])) 
    else:
        logger.info('VPC RT: No new routes for update') 

def get_ec2_instance_id(instance_tag):
    region = os.environ['AWS_REGION']
    ec2 = boto3.client('ec2', region_name=region)
    filters = [{"Name":"tag:MerakiTag", "Values":[instance_tag]}]
    instances = ec2.describe_instances(Filters=filters)
    instance_id = []
    logger.info('AWS EC2: Checking for vMX instances with instance tag {0}'.format(instance_tag))
    for i in instances['Reservations']:
        instance_id.append(i['Instances'][0]['InstanceId'])
        if i['Instances'][0]['State']['Name'] == 'running':
            logger.info('AWS EC2: Running vMX instance found with tag {0} and instance id {1}'.format(instance_tag, instance_id))
        else:
            logger.info('AWS EC2: Shutdown/Terminated vMX instance found with instance tag {0} and instance id {1}'.format(instance_tag, i['Instances'][0]['InstanceId']))
    if len(instance_id) > 1:
        logger.error('AWS EC2: More that one running instance with the same tag, please remove tag from stale/broken instance')
        logger.error('AWS EC2: The following instances {0}, were found with the tag {1}'.format(instance_id, instance_tag))
        exit
    else:
        return instance_id[0]

def update_rt():
    org_id = ORG_ID
    vmx1_tag = VMX1_TAG
    vmx2_tag = VMX2_TAG
    meraki_api_key = get_meraki_key()
    meraki_dashboard = meraki.DashboardAPI(meraki_api_key, suppress_logging=True)
    #get vmx ec2 instance ids using tags
    ec2_vmx1_id = get_ec2_instance_id(vmx1_tag)
    ec2_vmx2_id = get_ec2_instance_id(vmx2_tag)
    #get corresponding vmx network ids using tags
    meraki_vmx1_id = get_meraki_tagged_networks(meraki_dashboard, org_id, vmx1_tag)
    meraki_vmx2_id = get_meraki_tagged_networks(meraki_dashboard, org_id, vmx2_tag)
    #get autovpn branch site routes for the vMXs 
    vpn_routes = get_all_vpn_routes(meraki_dashboard, org_id, meraki_vmx1_id, meraki_vmx2_id)
    ##update TGW routes
    for routes in vpn_routes: update_tgw_rt(routes, TGW_RT_ID, TGW_ATTACH_ID)
    ##check vmx status
    if ec2_vmx1_id and ec2_vmx1_id and meraki_vmx1_id and meraki_vmx1_id:
        vmx1_status = check_vmx_status(meraki_dashboard, org_id, meraki_vmx1_id, ec2_vmx1_id)
        vmx2_status = check_vmx_status(meraki_dashboard, org_id, meraki_vmx2_id, ec2_vmx2_id)
    else:
        logger.error('vMX Instance Ids: No vMXs instance IDs found')
        exit
    #update VPC route tables based on vMX instance state
    if vmx1_status == 'online' and vmx2_status == 'online':
        logger.info('vMX Status: vmx1 and vmx2 are both online')
        logger.info('VPC RT Update: Updating VPC route table for vMX1')
        update_vpc_rt(vpn_routes[0], ec2_vmx1_id, RT_ID)
        logger.info('VPC RT Update: Updating VPC route table for vMX2')
        update_vpc_rt(vpn_routes[1], ec2_vmx2_id, RT_ID)
    elif vmx1_status == 'online' and vmx2_status == 'offline':
        logger.info ("vMX Status: vmx1 online and vmx2 offline, moving all routes to vmx1")
        logger.info('VPC RT Update: Updating VPC route table for vMX1')
        update_vpc_rt(vpn_routes[0], ec2_vmx1_id, RT_ID)
        update_vpc_rt(vpn_routes[1], ec2_vmx1_id, RT_ID)
    elif vmx1_status == 'offline' and vmx2_status == 'online':
        logger.info ("vMX Status: vmx2 online and vmx1 offline, moving all routes to vmx2")
        logger.info('VPC RT Update: Updating VPC route table for vMX2')
        update_vpc_rt(vpn_routes[0], ec2_vmx2_id, RT_ID)
        update_vpc_rt(vpn_routes[1], ec2_vmx2_id, RT_ID)
    else:
        logger.info ('vMX1 and vMX2 are BOTH offline')
        #TODO: Cloudwatch enhancement to generate alerts when both vMXs are offline

def main(event, context):
    # This lambda function monitors the state of the vMX instances and updates the SDWAN VPC and TGW route tables accordingly.
    # The function gets instatiated on a periodic Cloudwatch event, the frequency of the periodic check is configurable and taken as an input for the cft templates. 

    try:
        logger.info('Lambda Execution: Executed on event {0}'.format(event))
        update_rt()
    except Exception as e:
        logging.error('Exception: %s' % e, exc_info=True)

if __name__ == "__main__":   
    main('', '')
