import os
import requests
import meraki
import boto3
import botocore
import logging
import json
import sys

from botocore.exceptions import ClientError

logging.basicConfig(stream = sys.stdout)
logger = logging.getLogger()
logger.setLevel(logging.INFO)

ORG_ID = os.environ['MERAKI_ORG_ID']
TGW_RT_ID = os.environ['TGW_RT_ID']
TGW_ATTACH_ID = os.environ['TGW_ATTACH_ID']
EC2_VMX1_ID = os.environ['VMX1_ID']
EC2_VMX2_ID = os.environ['VMX2_ID']
RT_ID = os.environ['RT_ID']

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
            merakiapikey = text_secret_data['merakiapikey']
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
        if networks['vpnMode'] == 'spoke' and networks['merakiVpnPeers'][0]['networkId'] == vmx1_id:
            for subnets in networks['exportedSubnets']:
                vpn_routes_vmx1.append(subnets.get('subnet'))
    
        elif networks['vpnMode'] == 'spoke' and networks['merakiVpnPeers'][0]['networkId'] == vmx2_id:
            for subnets in networks['exportedSubnets']:
                vpn_routes_vmx2.append(subnets.get('subnet'))
        else:
            logger.info('Meraki Dashboard: No routes found for vMX Hubs')
            pass 
    return vpn_routes_vmx1, vpn_routes_vmx2

def get_tagged_networks(dashboard, org_id):
    vmx1tag = 'vmx1'
    vmx2tag = 'vmx2'
    # executing API call to obtain all Meraki networks in the organization
    organization_networks_response = dashboard.organizations.getOrganizationNetworks(
        org_id, total_pages='all'
    )
    vmx1 = [x for x in organization_networks_response if str(vmx1tag) in str(x['tags'])[1:-1]]
    vmx2 = [x for x in organization_networks_response if str(vmx2tag) in str(x['tags'])[1:-1]] 

    return vmx1[0]['id'], vmx2[0]['id']

def check_vmx_status(dashboard, org_id, vmx_id, ec2_vmx1_id):
    region = os.environ['AWS_REGION']
    ec2 = boto3.client('ec2', region_name=region) 
    org_device_status = dashboard.organizations.getOrganizationDevicesStatuses(
        org_id, total_pages='all'
    )
    meraki_vmx_status = [x for x in org_device_status if str(vmx_id) in str(x['networkId'])][0]['status']
    ec2_vmx_status = ec2.describe_instance_status(InstanceIds=[ec2_vmx1_id])
    if meraki_vmx_status == 'online' and ec2_vmx_status['InstanceStatuses'][0]['InstanceState']['Name'] == 'running':
        vmx_status = 'online'
    else:
        vmx_status ='offline'
    
    return vmx_status
            
def update_tgw_rt(vpn_routes, tgw_rt_id, tgw_attach_id):
    region = os.environ['AWS_REGION']
    ec2 = boto3.client('ec2', region_name=region)
    #Checking if the route already exsists, if so skip updating the TGW route table
    for route in vpn_routes:
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
        logger.info('VPC RT: No new routes for update') 

def main(event, context):
    meraki_api_key = get_meraki_key()
    meraki_dashboard = meraki.DashboardAPI(meraki_api_key, suppress_logging=True)
    org_id = ORG_ID
    #get vmx ids using tags
    vmxids= get_tagged_networks(meraki_dashboard, org_id)
    vpn_routes = get_all_vpn_routes(meraki_dashboard, org_id, vmxids[0], vmxids[1])
    # Update TGW routes
    for routes in vpn_routes: update_tgw_rt(routes, TGW_RT_ID, TGW_ATTACH_ID)
    vmx1_status = check_vmx_status(meraki_dashboard, org_id, vmxids[0], EC2_VMX1_ID)
    vmx2_status = check_vmx_status(meraki_dashboard, org_id, vmxids[1], EC2_VMX2_ID)
    if vmx1_status == 'online' and vmx2_status == 'online':
        logger.info('vMX Status: vmx1 and vmx2 are both online')
        logger.info('VPC RT Update: Updating VPC route table for vMX1')
        update_vpc_rt(vpn_routes[0], EC2_VMX1_ID, RT_ID)
        logger.info('VPC RT Update: Updating VPC route table for vMX2')
        update_vpc_rt(vpn_routes[1], EC2_VMX2_ID, RT_ID)
    elif vmx1_status == 'online' and vmx2_status == 'offline':
        logger.info ("vMX Status: vmx1 online and vmx2 offline, moving all routes to vmx1")
        logger.info('VPC RT Update: Updating VPC route table for vMX1')
        update_vpc_rt(vpn_routes[0], EC2_VMX1_ID, RT_ID)
        update_vpc_rt(vpn_routes[1], EC2_VMX1_ID, RT_ID)
    elif vmx1_status == 'offline' and vmx2_status == 'online':
        logger.info ("vMX Status: vmx2 online and vmx1 offline, moving all routes to vmx2")
        logger.info('VPC RT Update: Updating VPC route table for vMX2')
        update_vpc_rt(vpn_routes[0], EC2_VMX2_ID, RT_ID)
        update_vpc_rt(vpn_routes[1], EC2_VMX2_ID, RT_ID)
    else:
        logger.info ('vMX1 and vMX2 are BOTH offline')
        #TODO: Cloudwatch enhancement to generate alerts when both vMXs are offline   
    #return vpn_routes

if __name__ == "__main__":   
    main('', '')