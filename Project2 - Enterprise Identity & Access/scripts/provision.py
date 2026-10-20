"""Simulated customer provisioning client. Reads tokens locally; never prints them."""
import argparse
import json
from pathlib import Path
import sys
from dotenv import dotenv_values
import requests


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tenant',required=True,choices=['acme','globex'])
    sub=parser.add_subparsers(dest='action',required=True)
    sub.add_parser('list')
    user=sub.add_parser('create-user')
    user.add_argument('--username',required=True)
    user.add_argument('--subject',required=True,help='Exact Auth0 user_id / OIDC sub, not email')
    for action in ('activate','deactivate','promote','demote'):
        cmd=sub.add_parser(action)
        cmd.add_argument('--user-id',required=True,help='Application SCIM resource id from list')
    args=parser.parse_args()
    config=dotenv_values(Path(__file__).resolve().parents[1]/'.env')
    token=json.loads(config.get('SCIM_TOKENS_JSON','{}')).get(args.tenant)
    if not token:
        sys.exit('No SCIM token configured for that tenant in .env.')
    base=config.get('BASE_URL','http://localhost:5002')+'/scim/v2/'
    def call(method,path,payload=None,params=None):
        response=requests.request(method,base+path,headers={'Authorization':'Bearer '+token},json=payload,params=params,timeout=15)
        if not response.ok:
            sys.exit(f'Provisioning failed ({response.status_code}): {response.text}')
        return response.json() if response.content else {}
    if args.action=='list':
        result=call('GET','Users')
    elif args.action=='create-user':
        result=call('POST','Users',{'schemas':['urn:ietf:params:scim:schemas:core:2.0:User'],'userName':args.username,'externalId':args.subject,'active':True})
    elif args.action in ('activate','deactivate'):
        result=call('PATCH','Users/'+args.user_id,{'Operations':[{'op':'replace','path':'active','value':args.action=='activate'}]})
    else:
        groups=call('GET','Groups',params={'filter':'displayName eq "Managers"'})['Resources']
        if not groups:
            groups=[call('POST','Groups',{'displayName':'Managers','members':[]})]
        result=call('PATCH','Groups/'+groups[0]['id'],{'Operations':[{'op':'add' if args.action=='promote' else 'remove','path':'members','value':[{'value':args.user_id}]}]})
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    main()
