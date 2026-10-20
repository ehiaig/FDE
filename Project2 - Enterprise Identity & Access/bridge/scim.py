"""A deliberately bounded SCIM 2.0 Users/Groups implementation (see README)."""
import re
import secrets
from uuid import UUID, uuid4

import psycopg
from flask import Blueprint, abort, current_app, g, jsonify, request
from werkzeug.exceptions import HTTPException
from .db import transaction

bp = Blueprint('scim', __name__, url_prefix='/scim/v2')
CORE = 'urn:ietf:params:scim:schemas:core:2.0:'
API = 'urn:ietf:params:scim:api:messages:2.0:'


@bp.before_request
def authenticate():
    supplied = request.headers.get('Authorization', '')
    g.scim_tenant = next((t for t, token in current_app.config['SCIM_TOKENS'].items()
                          if secrets.compare_digest(supplied, 'Bearer ' + token)), None)
    if not g.scim_tenant:
        abort(401, 'A valid tenant provisioning token is required.')


@bp.after_request
def content_type(response):
    response.headers['Content-Type'] = 'application/scim+json'
    return response


@bp.errorhandler(HTTPException)
def error(error):
    return jsonify(schemas=[API+'Error'], status=str(error.code), detail=error.description), error.code


@bp.errorhandler(psycopg.IntegrityError)
def conflict(error):
    return jsonify(schemas=[API+'Error'], status='409', detail='Duplicate identity or invalid membership.'), 409


def data():
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        abort(400, 'Expected a JSON object.')
    return body


def identifier(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError):
        abort(400, 'Invalid resource identifier.')


def required_string(body, key):
    value = body.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > 255:
        abort(400, f'{key} must be a nonempty string of at most 255 characters.')
    return value


def active_value(body):
    value = body.get('active', True)
    if type(value) is not bool:
        abort(400, 'active must be a boolean.')
    return value


def get_row(conn, kind, rid):
    table = 'users' if kind == 'Users' else 'groups'
    row = conn.execute(f'SELECT * FROM {table} WHERE id=%s', (identifier(rid),)).fetchone()
    if not row:
        abort(404, 'Resource not found.')
    return row


def resource(conn, kind, row):
    result = {'schemas':[CORE+kind[:-1]], 'id':str(row['id']),
              'meta':{'resourceType':kind[:-1]}}
    if kind == 'Users':
        result.update(userName=row['username'], externalId=row['external_id'], active=row['active'])
        result['groups'] = [{'value':str(r['id']), 'display':r['name']} for r in conn.execute('SELECT g.id,g.name FROM groups g JOIN memberships m ON m.group_id=g.id AND m.tenant=g.tenant WHERE m.user_id=%s ORDER BY g.name', (row['id'],))]
    else:
        result.update(displayName=row['name'], members=[{'value':str(r['user_id'])} for r in conn.execute('SELECT user_id FROM memberships WHERE group_id=%s ORDER BY user_id', (row['id'],))])
    return result


def members(conn, group_id, values, replace=False, remove=False):
    if not isinstance(values, list) or len(values) > 1000:
        abort(400, 'members must be an array with at most 1000 entries.')
    ids = []
    for value in values:
        if not isinstance(value, dict) or 'value' not in value:
            abort(400, 'Every member needs a value.')
        uid = identifier(value['value'])
        get_row(conn, 'Users', uid)  # Foreign tenant IDs cannot be linked.
        ids.append(uid)
    if replace:
        conn.execute('DELETE FROM memberships WHERE group_id=%s', (group_id,))
    for uid in ids:
        if remove:
            conn.execute('DELETE FROM memberships WHERE group_id=%s AND user_id=%s', (group_id, uid))
        else:
            conn.execute('INSERT INTO memberships(tenant,group_id,user_id) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING', (g.scim_tenant,group_id,uid))


@bp.get('/ServiceProviderConfig')
def config():
    return jsonify(schemas=[CORE+'ServiceProviderConfig'], patch={'supported':True},
                   bulk={'supported':False,'maxOperations':0,'maxPayloadSize':0},
                   filter={'supported':True,'maxResults':100}, changePassword={'supported':False},
                   sort={'supported':False}, etag={'supported':False},
                   authenticationSchemes=[{'type':'oauthbearertoken','name':'Tenant bearer token','description':'A separate secret for each customer.'}])


@bp.get('/ResourceTypes')
def types():
    return jsonify(schemas=[API+'ListResponse'], totalResults=2, startIndex=1, itemsPerPage=2,
                   Resources=[{'schemas':[CORE+'ResourceType'],'id':k,'name':k,'endpoint':'/'+k+'s','schema':CORE+k} for k in ('User','Group')])


@bp.route('/<kind>', methods=['GET', 'POST'])
def collection(kind):
    if kind not in ('Users','Groups'):
        abort(404)
    table = 'users' if kind == 'Users' else 'groups'
    with transaction(g.scim_tenant) as conn:
        if request.method == 'GET':
            where, params = '', []
            if request.args.get('filter'):
                match = re.fullmatch(r'(userName|externalId|displayName) eq "([^"\\]*)"', request.args['filter'])
                fields = {'userName':'username','externalId':'external_id'} if kind == 'Users' else {'displayName':'name'}
                if not match or match[1] not in fields:
                    abort(400, 'Supported filters: userName/externalId/displayName eq "value".')
                where, params = ' WHERE '+fields[match[1]]+'=%s', [match[2]]
            try:
                start, count = max(1,int(request.args.get('startIndex',1))), min(100,max(0,int(request.args.get('count',100))))
            except ValueError:
                abort(400, 'Invalid pagination.')
            total = conn.execute(f'SELECT count(*) AS n FROM {table}'+where, params).fetchone()['n']
            rows = conn.execute(f'SELECT * FROM {table}'+where+' ORDER BY id LIMIT %s OFFSET %s', params+[count,start-1]).fetchall()
            return jsonify(schemas=[API+'ListResponse'],totalResults=total,startIndex=start,itemsPerPage=len(rows),Resources=[resource(conn,kind,r) for r in rows])
        body = data()
        rid = str(uuid4())
        if kind == 'Users':
            conn.execute('INSERT INTO users(id,tenant,external_id,username,active) VALUES (%s,%s,%s,%s,%s)',
                         (rid,g.scim_tenant,required_string(body,'externalId'),required_string(body,'userName'),active_value(body)))
        else:
            conn.execute('INSERT INTO groups(id,tenant,name) VALUES (%s,%s,%s)', (rid,g.scim_tenant,required_string(body,'displayName')))
            members(conn,rid,body.get('members',[]))
        result = resource(conn,kind,get_row(conn,kind,rid))
    response = jsonify(result)
    response.status_code = 201
    response.headers['Location'] = current_app.config['BASE_URL']+'/scim/v2/'+kind+'/'+rid
    return response


@bp.route('/<kind>/<rid>', methods=['GET','PUT','PATCH','DELETE'])
def item(kind,rid):
    if kind not in ('Users','Groups'):
        abort(404)
    table = 'users' if kind == 'Users' else 'groups'
    with transaction(g.scim_tenant) as conn:
        row = get_row(conn,kind,rid)
        rid = str(row['id'])
        if request.method == 'GET':
            return jsonify(resource(conn,kind,row))
        if request.method == 'DELETE':
            conn.execute(f'DELETE FROM {table} WHERE id=%s', (rid,))
            return '',204
        body = data()
        if request.method == 'PUT':
            if kind == 'Users':
                update_user(conn,rid,body,replace=True)
            else:
                conn.execute('UPDATE groups SET name=%s WHERE id=%s', (required_string(body,'displayName'),rid))
                members(conn,rid,body.get('members',[]),replace=True)
        else:
            operations = body.get('Operations')
            if not isinstance(operations,list) or not 1 <= len(operations) <= 100:
                abort(400, 'Expected 1–100 Operations.')
            for operation in operations:
                if not isinstance(operation,dict):
                    abort(400,'Invalid operation.')
                op = str(operation.get('op','')).lower()
                path, value = operation.get('path'), operation.get('value')
                if op not in ('add','replace','remove'):
                    abort(400,'Unsupported operation.')
                if kind == 'Users':
                    if op == 'remove':
                        abort(400,'Removing required user attributes is unsupported.')
                    values = value if path is None else {path:value} if isinstance(path,str) else None
                    if not isinstance(values,dict) or not values or set(values)-{'active','userName','externalId'}:
                        abort(400,'Supported user attributes: active, userName, externalId.')
                    update_user(conn,rid,values)
                elif path == 'displayName' and op in ('add','replace'):
                    conn.execute('UPDATE groups SET name=%s WHERE id=%s', (required_string({'displayName':value},'displayName'),rid))
                elif path == 'members':
                    if op == 'remove' and value is None:
                        conn.execute('DELETE FROM memberships WHERE group_id=%s', (rid,))
                    else:
                        members(conn,rid,value,replace=op=='replace',remove=op=='remove')
                elif isinstance(path,str) and re.fullmatch(r'members\[value eq "[0-9a-fA-F-]+"\]',path) and op=='remove':
                    members(conn,rid,[{'value':path.split('"')[1]}],remove=True)
                elif path is None and isinstance(value,dict) and op in ('add','replace') and value and not set(value)-{'displayName','members'}:
                    if 'displayName' in value:
                        conn.execute('UPDATE groups SET name=%s WHERE id=%s', (required_string(value,'displayName'),rid))
                    if 'members' in value:
                        members(conn,rid,value['members'],replace=op=='replace')
                else:
                    abort(400,'Unsupported group patch path.')
        return jsonify(resource(conn,kind,get_row(conn,kind,rid)))


def update_user(conn,rid,values,replace=False):
    if replace or 'userName' in values:
        conn.execute('UPDATE users SET username=%s WHERE id=%s',(required_string(values,'userName'),rid))
    if replace or 'externalId' in values:
        # Rebinding identity invalidates old sessions as well.
        external = required_string(values,'externalId')
        conn.execute('DELETE FROM sessions WHERE user_id=%s AND EXISTS (SELECT 1 FROM users WHERE id=%s AND external_id<>%s)',(rid,rid,external))
        conn.execute('UPDATE users SET external_id=%s WHERE id=%s',(external,rid))
    if replace or 'active' in values:
        active = active_value(values)
        conn.execute('UPDATE users SET active=%s WHERE id=%s',(active,rid))
        if not active:
            conn.execute('DELETE FROM sessions WHERE user_id=%s',(rid,))
