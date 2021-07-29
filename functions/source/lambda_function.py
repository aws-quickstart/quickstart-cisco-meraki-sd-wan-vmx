import os
import requests
import meraki
import boto3
import botocore
import logging

API_KEY = os.environ['MERAKI_API_KEY']
ORG_NAME = os.environ['MERAKI_ORG_NAME']
TGW_RT_ID = os.environ['TGW_RT_ID']
TGW_ATTACH_ID = os.environ['TGW_ATTACH_ID']
VMX1_ID = os.environ['VMX1_ID']
VMX2_ID = os.environ['VMX2_ID']
RT_ID = os.environ['RT_ID']

def meraki_connector(api_key, org_name):
   # creating authentication variable for the Meraki SDK
    meraki_dashboard_sdk_auth = meraki.DashboardAPI(API_KEY, suppress_logging=True)

    # writing function to obtain org ID via linking ORG name
    result_org_id = meraki_dashboard_sdk_auth.organizations.getOrganizations()
    for org in result_org_id:
        if org['name'] == ORG_NAME:
            org_id = org['id']
    return meraki_dashboard_sdk_auth, org_id


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

def check_vmx_status(dashboard, org_id, vmx_id):
    org_device_status = dashboard.organizations.getOrganizationDevicesStatuses(
        org_id, total_pages='all'
    )
    vmx_status = [x for x in org_device_status if str(vmx_id) in str(x['networkId'])][0]['status']

    return vmx_status
            
def update_tgw_rt(vpn_routes, tgw_rt_id, tgw_attach_id):
    region = os.environ['AWS_REGION']
    print(region, tgw_rt_id, tgw_attach_id)
    ec2 = boto3.client('ec2', region_name=region)
    for route in vpn_routes:
        try:
            ec2.create_transit_gateway_route(
            DestinationCidrBlock= route,
            TransitGatewayRouteTableId=tgw_rt_id,
            TransitGatewayAttachmentId=tgw_attach_id
           )
        except botocore.exceptions.ClientError as error:
            if error.response['Error']['Code'] == 'RouteAlreadyExists':
                print("TGW Route already exsists, updating it", route)
                ec2.replace_transit_gateway_route(
                DestinationCidrBlock= route,
                TransitGatewayRouteTableId=tgw_rt_id,
                TransitGatewayAttachmentId=tgw_attach_id
                )
            else:
                raise error

def update_vpc_rt(vpn_routes, vmx_id, rt_id):
    region = os.environ['AWS_REGION']
    #region = 'us-east-1'
    ec2 = boto3.client('ec2', region_name=region)
    uniq_vpn_routes = list(set(vpn_routes))
    print(uniq_vpn_routes)
    instance_id = vmx_id
    for route in uniq_vpn_routes:
        try:
            ec2.create_route(
                DestinationCidrBlock=route,
                InstanceId=instance_id,
                RouteTableId=rt_id
            )
        except botocore.exceptions.ClientError as error:
            if error.response['Error']['Code'] == 'RouteAlreadyExists':
                print("VPC Route already exsists, updating it", route)
                ec2.replace_route(
                DestinationCidrBlock=route,
                InstanceId=instance_id,
                RouteTableId=rt_id
                )
            else:
                raise error 

def main(event, context):
    meraki_auth = meraki_connector(API_KEY, ORG_NAME)
    meraki_dashboard = meraki_auth[0]
    org_id = meraki_auth[1]
    #get vmx ids using tags
    vmxids= get_tagged_networks(meraki_dashboard, org_id)
    print(vmxids[0], vmxids[1])
    vpn_routes = get_all_vpn_routes(meraki_dashboard, org_id, vmxids[0], vmxids[1])
    for routes in vpn_routes: update_tgw_rt(routes, TGW_RT_ID, TGW_ATTACH_ID)
    vmx1_status = check_vmx_status(meraki_dashboard, org_id, vmxids[0])
    vmx2_status = check_vmx_status(meraki_dashboard, org_id, vmxids[1])
    if vmx1_status == 'online' and vmx2_status == 'online':
        print("both vmxs are online")
        update_vpc_rt(vpn_routes[0], VMX1_ID, RT_ID)
        update_vpc_rt(vpn_routes[1], VMX2_ID, RT_ID)
    elif vmx1_status == 'online' and vmx2_status == 'offline':
        print ("vmx1 online and vmx2 offline, moving all routes to vmx1")
        update_vpc_rt(vpn_routes[0], VMX1_ID, RT_ID)
        update_vpc_rt(vpn_routes[1], VMX1_ID, RT_ID)
    elif vmx1_status == 'offline' and vmx2_status == 'online':
        print ("vmx2 online and vmx1 is offline")
        update_vpc_rt(vpn_routes[0], VMX2_ID, RT_ID)
        update_vpc_rt(vpn_routes[1], VMX2_ID, RT_ID)
    else:
        print ("both vmxs are offline") 
    
    #return vpn_routes

if __name__ == "__main__":   
    main('', '')