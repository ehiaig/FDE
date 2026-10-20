from conftest import scim,login,query


def test_auth_and_cross_tenant_isolation(client):
    assert client.get('/scim/v2/Users').status_code==401
    users=scim(client,'Users').json['Resources']
    assert len(users)==2
    assert all('@acme.' in u['userName'] for u in users)
    assert scim(client,'Users/20000000-0000-0000-0000-000000000001').status_code==404
    assert scim(client,'Users/20000000-0000-0000-0000-000000000001','delete').status_code==404
    assert scim(client,'Users?filter=userName%20eq%20%22alice@acme.example%22').json['totalResults']==1
    assert scim(client,'Users?filter=userName%20co%20%22alice%22').status_code==400
    assert scim(client,'Users?count=0').json['Resources']==[]


def test_user_group_lifecycle(client):
    user=scim(client,'Users','post',body={'userName':'new@acme.example','externalId':'auth0|new','active':True})
    assert user.status_code==201
    uid=user.json['id']
    group=scim(client,'Groups','post',body={'displayName':'Engineering','members':[{'value':uid}]})
    assert group.status_code==201
    gid=group.json['id']
    assert scim(client,'Users/'+uid).json['groups'][0]['value']==gid
    updated=scim(client,'Users/'+uid,'put',body={'userName':'renamed@acme.example','externalId':'auth0|new','active':False})
    assert updated.status_code==200 and not updated.json['active']
    assert scim(client,'Groups/'+gid,'put',body={'displayName':'Research','members':[]}).status_code==200
    assert scim(client,'Groups/'+gid).json['members']==[]
    assert scim(client,'Users/'+uid,'delete').status_code==204
    assert scim(client,'Users/'+uid).status_code==404
    assert scim(client,'Groups/'+gid,'delete').status_code==204


def test_bad_membership_rolls_back_entire_patch(client):
    group='Groups/10000000-0000-0000-0000-000000000010'
    response=scim(client,group,'patch',body={'Operations':[
        {'op':'replace','path':'displayName','value':'Changed'},
        {'op':'add','path':'members','value':[{'value':'20000000-0000-0000-0000-000000000001'}]}
    ]})
    assert response.status_code==404
    assert scim(client,group).json['displayName']=='Managers'


def test_invalid_data_and_duplicate(client):
    assert scim(client,'Users','post',body={'userName':'a','externalId':'a','active':'false'}).status_code==400
    assert scim(client,'Users','post',body={'userName':'alice@acme.example','externalId':'new'}).status_code==409
    response=scim(client,'Users/not-a-uuid')
    assert response.status_code==400 and response.json['status']=='400'
    assert response.content_type=='application/scim+json'


def test_group_promotion_and_identity_rebind(client):
    login(client,'alice')
    uid='10000000-0000-0000-0000-000000000001'
    group='Groups/10000000-0000-0000-0000-000000000010'
    assert scim(client,group,'patch',body={'Operations':[{'op':'add','path':'members','value':[{'value':uid}]}]}).status_code==200
    assert len(query(client).json['sources'])==1
    assert scim(client,'Users/'+uid,'patch',body={'Operations':[{'op':'replace','value':{'externalId':'auth0|new'}}]}).status_code==200
    assert client.get('/api/me').status_code==401
