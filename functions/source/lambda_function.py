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


def get_all_vpn_routes(dashboard, org_id):
    org_vpn_status = dashboard.appliance.getOrganizationApplianceVpnStatuses(
    org_id, total_pages='all'
    )
    vpn_routes = []
    for networks in org_vpn_status:
        for subnets in networks['exportedSubnets']:
            vpn_routes.append(subnets.get('subnet'))
    
    return vpn_routes

def update_tgw_rt(vpn_routes, tgw_rt_id, tgw_attach_id):
    region = os.environ['AWS_REGION']
    print(region, tgw_rt_id, tgw_attach_id)
    ec2 = boto3.client('ec2', region_name=region)
    vpn_routes = vpn_routes
    tgw_rt_id = tgw_rt_id
    #Removing duplicates from the vpn routes list
    uniq_vpn_routes = list(set(vpn_routes))
    print(uniq_vpn_routes)
    for route in uniq_vpn_routes:
        try:
            ec2.create_transit_gateway_route(
            DestinationCidrBlock= route,
            TransitGatewayRouteTableId=tgw_rt_id,
            TransitGatewayAttachmentId=tgw_attach_id
           )
        except botocore.exceptions.ClientError as error:
            if error.response['Error']['Code'] == 'RouteAlreadyExists':
                print("Route already exsists:", route)
            else:
                raise error

def update_vpc_rt(vpn_routes, vmx1_id, vmx2_id, rt_id):
    region = os.environ['AWS_REGION']
    #region = 'us-east-1'
    ec2 = boto3.client('ec2', region_name=region)
    uniq_vpn_routes = list(set(vpn_routes))
    print(uniq_vpn_routes)
    #TODO: add function to check vmx status, for now assuming vmx-1 is always up
    instance_id = vmx1_id 
    for route in uniq_vpn_routes:
        try:
            ec2.create_route(
                DestinationCidrBlock=route,
                InstanceId=instance_id,
                RouteTableId=rt_id
            )
        except botocore.exceptions.ClientError as error:
            if error.response['Error']['Code'] == 'RouteAlreadyExists':
                print("Route already exsists", route)
            else:
                raise error 

def main(event, context):
    meraki_auth = meraki_connector(API_KEY, ORG_NAME)
    meraki_dashboard = meraki_auth[0]
    org_id = meraki_auth[1]
    vpn_routes = get_all_vpn_routes(meraki_dashboard, org_id)
    print(vpn_routes)
    print("Calling update tgw_rts")
    update_tgw_rt(vpn_routes, TGW_RT_ID, TGW_ATTACH_ID)
    update_vpc_rt(vpn_routes, VMX1_ID, VMX2_ID, RT_ID)

    return vpn_routes

if __name__ == "__main__":   
    main('', '')