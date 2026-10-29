"""Zero Latency EV charging API. MySQL 8+, FastAPI."""
from __future__ import annotations
import os, uuid, math, datetime as dt, json, urllib.parse, urllib.request, secrets, hashlib, hmac, re
from pathlib import Path
from decimal import Decimal
from typing import Optional

import bcrypt, jwt, mysql.connector
from mysql.connector import pooling
from fastapi import FastAPI, HTTPException, Depends, Header, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, EmailStr

DB_CONFIG = dict(host=os.getenv('DB_HOST','127.0.0.1'), port=int(os.getenv('DB_PORT','3306')),
                 user=os.getenv('DB_USER','root'), password=os.getenv('DB_PASSWORD',''),
                 database=os.getenv('DB_NAME','ev_charging'), autocommit=False)
pool = pooling.MySQLConnectionPool(pool_name='zero_latency', pool_size=8, **DB_CONFIG)
SECRET = os.getenv('JWT_SECRET','change-this-secret-before-deploying')
OTP_HASH_SECRET = os.getenv('OTP_HASH_SECRET', SECRET).encode()
OTP_RESEND_COOLDOWN_SECONDS = 180
TOKEN_HOURS = int(os.getenv('TOKEN_HOURS','24'))
app = FastAPI(title='Zero Latency EV Charging API', version='1.0.0')
origins = [x.strip() for x in os.getenv('CORS_ORIGINS','*').split(',') if x.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=origins != ['*'], allow_methods=['*'], allow_headers=['*'])

def db():
    conn=pool.get_connection()
    try: yield conn
    finally: conn.close()
def q(conn, sql, args=(), one=False):
    cur=conn.cursor(dictionary=True)
    try:
        cur.execute(sql,args); return cur.fetchone() if one else cur.fetchall()
    finally: cur.close()
def exec_sql(conn, sql, args=()):
    cur=conn.cursor()
    try: cur.execute(sql,args); return cur.lastrowid
    finally: cur.close()
def uid(): return str(uuid.uuid4())
def begin(conn):
    # The auth dependency already ran a SELECT on this connection, which opened an implicit
    # transaction (autocommit is off). End it so start_transaction() does not fail.
    if conn.in_transaction: conn.rollback()
    conn.start_transaction()
def plain(v):
    if isinstance(v, dt.timedelta):
        t=int(v.total_seconds()); return f"{t//3600:02d}:{t%3600//60:02d}:{t%60:02d}"
    if isinstance(v, Decimal): return float(v)
    if isinstance(v,(dt.datetime,dt.date,dt.time)): return v.isoformat(sep=' ') if isinstance(v,dt.datetime) else v.isoformat()
    if isinstance(v,dict): return {k:plain(x) for k,x in v.items()}
    if isinstance(v,list): return [plain(x) for x in v]
    return v
def token_for(user,conn,user_agent=''):
    session_id=uid()
    exec_sql(conn,'INSERT INTO login_sessions(id,user_id,user_agent) VALUES(%s,%s,%s)',(session_id,user['id'],user_agent[:255]))
    return jwt.encode({'sub':user['id'],'sid':session_id,'exp':dt.datetime.now(dt.timezone.utc)+dt.timedelta(hours=TOKEN_HOURS)},SECRET,algorithm='HS256')
def current_user(authorization: Optional[str]=Header(None), conn=Depends(db)):
    if not authorization or not authorization.lower().startswith('bearer '): raise HTTPException(401,'Sign in is required')
    try:
        claims=jwt.decode(authorization.split(' ',1)[1],SECRET,algorithms=['HS256'])
        uid_=claims['sub'];session_id=claims['sid']
    except jwt.PyJWTError: raise HTTPException(401,'Invalid or expired token')
    except KeyError:raise HTTPException(401,'Session has expired. Sign in again.')
    session=q(conn,'SELECT id FROM login_sessions WHERE id=%s AND user_id=%s AND revoked_at IS NULL',(session_id,uid_),True)
    if not session:raise HTTPException(401,'This session has been signed out. Sign in again.')
    user=q(conn,'SELECT id,name,email,role,banned_at FROM users WHERE id=%s AND deleted_at IS NULL',(uid_,),True)
    if not user: raise HTTPException(401,'Account no longer exists')
    if user['banned_at']: raise HTTPException(403,'This account is banned after receiving three host strikes')
    user.pop('banned_at',None)
    user['_session_id']=session_id
    return user
def role(wanted):
    def check(user=Depends(current_user)):
        if user['role'] != wanted: raise HTTPException(403,f'{wanted.title()} account required')
        return user
    return check
class Register(BaseModel): name:str=Field(min_length=1,max_length=100); email:EmailStr; password:str=Field(min_length=8,max_length=128); role:str='consumer'; terms_accepted:bool
class Login(BaseModel): email:EmailStr; password:str
class StationIn(BaseModel): title:str=Field(min_length=2,max_length=100); connector_type:str=Field(min_length=1,max_length=50); pricing_type:str='per_kwh'; price_rate:float=Field(gt=0); latitude:float=Field(ge=-90,le=90); longitude:float=Field(ge=-180,le=180); address:Optional[str]=Field(None,min_length=3,max_length=250)
class StationUpdate(BaseModel): address:Optional[str]=Field(None,min_length=3,max_length=250); pricing_type:str; price_rate:float=Field(gt=0); latitude:Optional[float]=Field(None,ge=-90,le=90); longitude:Optional[float]=Field(None,ge=-180,le=180)
class StationStatusIn(BaseModel): status:str
class StrikeIn(BaseModel): reason:str=Field(min_length=5,max_length=500)
class BookingCreate(BaseModel): slot_id:str=Field(min_length=1,max_length=36); phone_number:str=Field(min_length=8,max_length=20); vehicle_registration:str=Field(min_length=4,max_length=20)
class VerifyBookingOtpIn(BaseModel): otp_code:str=Field(pattern=r'^\d{6}$')
class SlotIn(BaseModel): start_time:dt.datetime; end_time:dt.datetime
class RuleIn(BaseModel): label:str=Field(min_length=1,max_length=80); start_time:dt.time; end_time:dt.time; multiplier:float=Field(gt=0,le=5); active:bool=True
class StopIn(BaseModel): energy_kwh:float=Field(ge=0,le=10000)
class SessionInterruptionIn(BaseModel): reason:str=Field(min_length=1,max_length=40); description:Optional[str]=Field(None,max_length=1000)
class ReviewIn(BaseModel): rating:int=Field(ge=1,le=5); comment:str=Field(default='',max_length=1200)
class ReportIn(BaseModel): category:str='not_working'; description:str=Field(min_length=5,max_length=1200)
class AssistantIn(BaseModel): query:str; latitude:Optional[float]=None; longitude:Optional[float]=None; connector_type:Optional[str]=None; energy_kwh:float=Field(default=20,gt=0)

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
UTC = dt.timezone.utc

def slot_datetime_to_utc(value:dt.datetime)->dt.datetime:
    if value.tzinfo is None:
        value=value.replace(tzinfo=IST)
    return value.astimezone(UTC).replace(tzinfo=None)

def normalize_phone(value):
    digits=re.sub(r'[\s().-]','',value)
    if digits.startswith('+91'):
        digits=digits[3:]
    elif digits.startswith('0') and len(digits)==11:
        digits=digits[1:]
    if not re.fullmatch(r'[6-9]\d{9}',digits):
        raise HTTPException(422,'Enter a valid Indian mobile number (10 digits, optionally with +91)')
    return '+91'+digits
def normalize_vehicle_registration(value):
    plate=re.sub(r'[\s-]','',value).upper()
    if not (re.fullmatch(r'[A-Z]{2}\d{1,2}[A-Z]{1,3}\d{1,4}',plate) or re.fullmatch(r'\d{2}BH\d{4}[A-Z]{1,2}',plate)):
        raise HTTPException(422,'Enter a valid Indian vehicle registration number')
    return plate
def new_otp(booking_id):
    code=f'{secrets.randbelow(1_000_000):06d}'
    digest=hmac.new(OTP_HASH_SECRET, f'{booking_id}:{code}'.encode(), hashlib.sha256).hexdigest()
    return code,digest
def otp_resend_seconds(last_sent_at, now):
    return max(0,math.ceil((last_sent_at+dt.timedelta(seconds=OTP_RESEND_COOLDOWN_SECONDS)-now).total_seconds()))
def otp_matches(booking_id, code, expected_hash):
    digest=hmac.new(OTP_HASH_SECRET, f'{booking_id}:{code}'.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest,expected_hash)
def demo_otp_delivery_status():
    return 'development'
def save_otp_delivery_status(conn,booking_id,status):
    exec_sql(conn,'UPDATE booking_consumer_verifications SET otp_delivery_status=%s WHERE booking_id=%s',(status,booking_id))
    conn.commit()
def reserve_otp_send(conn,consumer_id,now):
    exec_sql(conn,'INSERT IGNORE INTO consumer_otp_rate_limits(consumer_id,window_started_at,send_count) VALUES(%s,%s,0)',(consumer_id,now))
    limits=q(conn,'SELECT window_started_at,send_count FROM consumer_otp_rate_limits WHERE consumer_id=%s FOR UPDATE',(consumer_id,),True)
    window=limits['window_started_at']
    count=int(limits['send_count'])
    if window<now-dt.timedelta(hours=1):
        window=now
        count=0
    if count>=10:
        raise HTTPException(429,'This account has reached its hourly verification-code limit. Try again later.')
    exec_sql(conn,'UPDATE consumer_otp_rate_limits SET window_started_at=%s,send_count=%s WHERE consumer_id=%s',(window,count+1,consumer_id))

def station_info(conn, station_id, lat=None, lng=None, energy=20):
    s=q(conn,"""SELECT s.id,s.host_id,s.title,s.address,s.connector_type,s.pricing_type,s.price_rate,s.status,
       ST_Latitude(s.coordinates) latitude,ST_Longitude(s.coordinates) longitude,
       (SELECT COUNT(*) FROM time_slots t WHERE t.station_id=s.id AND t.is_booked=0 AND t.start_time>UTC_TIMESTAMP()) available_slots,
       (SELECT AVG(r.rating) FROM station_reviews r WHERE r.station_id=s.id) average_rating,
       (SELECT COUNT(*) FROM station_reviews r WHERE r.station_id=s.id) review_count,
       (SELECT COUNT(*) FROM station_reports rp WHERE rp.station_id=s.id AND rp.status IN ('open','investigating')) open_reports
    FROM charging_stations s WHERE s.id=%s AND s.deleted_at IS NULL""",(station_id,),True)
    if not s:return None
    rule=effective_rule(conn,station_id)
    rate=float(s['price_rate'])*(float(rule['multiplier']) if rule else 1)
    s['effective_rate']=rate;s['pricing_rule']=rule['label'] if rule else 'Standard rate'
    s['estimated_cost']=rate*energy if s['pricing_type']=='per_kwh' else rate*energy/7.2
    s['reliability_status']='Needs attention' if s['open_reports']>=3 else ('Community rated' if s['review_count'] else 'Not yet rated')
    if lat is not None and lng is not None:
        s['distance_km']=distance(lat,lng,float(s['latitude']),float(s['longitude']));s['distance_m']=s['distance_km']*1000
    else:s['distance_km']=None;s['distance_m']=None
    s['recommendation_score']=max(0,100-s['distance_km']*5 if s['distance_km'] is not None else 50)+float(s['average_rating'] or 0)*3+min(s['available_slots'],5)*2-(15 if s['reliability_status']=='Needs attention' else 0)
    return plain(s)
def distance(a,b,c,d):
    r=6371; x=math.radians(c-a);y=math.radians(d-b);z=math.sin(x/2)**2+math.cos(math.radians(a))*math.cos(math.radians(c))*math.sin(y/2)**2
    return 2*r*math.asin(math.sqrt(z))
def to_time(v):
    if isinstance(v, dt.timedelta):
        return (dt.datetime.min + v).time()
    return v
def effective_rule(conn,sid,lock=False):
    now=dt.datetime.utcnow().time()
    rules=q(conn,'SELECT * FROM station_pricing_rules WHERE station_id=%s AND active=1'+(' FOR UPDATE' if lock else ''),(sid,))
    for r in rules:
        start=to_time(r['start_time']);end=to_time(r['end_time'])
        if (start<=now<end if start<=end else now>=start or now<end): return plain(r)
    return None
def station_or_404(conn,sid):
    x=station_info(conn,sid)
    if not x: raise HTTPException(404,'Station not found')
    return x
def owned_station(conn,sid,user):
    s=station_or_404(conn,sid)
    if s['host_id']!=user['id']:raise HTTPException(403,'You can manage only your own stations')
    return s

@app.get('/health')
def health(conn=Depends(db)):
    q(conn,'SELECT 1',one=True);return {'status':'ok','database':'connected'}
@app.get('/geocode')
def geocode(address:str=Query(...,min_length=3,max_length=250)):
    url='https://nominatim.openstreetmap.org/search?'+urllib.parse.urlencode({'q':address,'format':'jsonv2','addressdetails':1,'countrycodes':'in','limit':5})
    request=urllib.request.Request(url,headers={'User-Agent':'ZeroLatencyEVCharging/1.0'})
    try:
        with urllib.request.urlopen(request,timeout=8) as response: matches=json.loads(response.read())
    except (OSError,json.JSONDecodeError) as exc: raise HTTPException(502,'Address lookup is temporarily unavailable') from exc
    if not matches: raise HTTPException(404,'No matching address found')
    matches=[match for match in matches if match.get('address',{}).get('country_code')=='in']
    if not matches:raise HTTPException(404,'No matching address found in India')
    indian_states=('andhra pradesh','arunachal pradesh','assam','bihar','chhattisgarh','goa','gujarat','haryana','himachal pradesh','jharkhand','karnataka','kerala','madhya pradesh','maharashtra','manipur','meghalaya','mizoram','nagaland','odisha','punjab','rajasthan','sikkim','tamil nadu','telangana','tripura','uttar pradesh','uttarakhand','west bengal','andaman and nicobar islands','chandigarh','dadra and nagar haveli and daman and diu','delhi','jammu and kashmir','ladakh','lakshadweep','puducherry')
    normalized_query=' '.join(address.lower().replace(',',' ').split())
    requested_state=next((state for state in sorted(indian_states,key=len,reverse=True) if state in normalized_query),None)
    if requested_state:
        matches=[match for match in matches if requested_state in ' '.join(str(match.get('address',{}).get(key,'')).lower() for key in ('state','state_district','region'))]
        if not matches:raise HTTPException(422,f'No result found in {requested_state.title()}. Check the address and try again.')
    match=matches[0]
    address_parts=match.get('address',{})
    display_name=match.get('display_name','')
    state_name=address_parts.get('state')
    if state_name and state_name.lower() not in display_name.lower():display_name=f'{display_name}, {state_name}'
    return {'latitude':float(match['lat']),'longitude':float(match['lon']),'display_name':display_name,'state':state_name}
@app.post('/auth/register')
def register(data:Register,request:Request,conn=Depends(db)):
    if not data.terms_accepted:raise HTTPException(400,'You must accept the Terms and Conditions to create an account')
    if data.role not in ('consumer','host'):raise HTTPException(422,'role must be consumer or host')
    if q(conn,'SELECT id FROM users WHERE email=%s',(data.email.lower(),),True):raise HTTPException(409,'An account with this email already exists')
    user={'id':uid(),'name':data.name.strip(),'email':data.email.lower(),'role':data.role}
    try:
        exec_sql(conn,'INSERT INTO users(id,name,email,password_hash,role) VALUES(%s,%s,%s,%s,%s)',(user['id'],user['name'],user['email'],bcrypt.hashpw(data.password.encode(),bcrypt.gensalt()).decode(),user['role']))
        access_token=token_for(user,conn,request.headers.get('user-agent',''))
        conn.commit()
    except Exception:conn.rollback();raise
    return {'token':access_token,'role':user['role']}
@app.post('/auth/login')
def login(data:Login,request:Request,conn=Depends(db)):
    row=q(conn,'SELECT * FROM users WHERE email=%s',(data.email.lower(),),True)
    if not row or not bcrypt.checkpw(data.password.encode(),row['password_hash'].encode()):raise HTTPException(401,'Email or password is incorrect')
    if row['deleted_at']:raise HTTPException(403,'This account is deactivated')
    if row['banned_at']:raise HTTPException(403,'This account is banned after receiving three host strikes')
    try:
        access_token=token_for(row,conn,request.headers.get('user-agent',''))
        conn.commit()
    except Exception:conn.rollback();raise
    return {'token':access_token,'role':row['role']}
@app.get('/auth/me')
def me(user=Depends(current_user)):
    return {key:value for key,value in user.items() if not key.startswith('_')}

@app.post('/auth/logout')
def auth_logout(conn=Depends(db),user=Depends(current_user)):
    exec_sql(conn,'UPDATE login_sessions SET revoked_at=UTC_TIMESTAMP() WHERE id=%s AND user_id=%s AND revoked_at IS NULL',(user['_session_id'],user['id']))
    conn.commit()
    return {'signed_out':True}

@app.get('/stations/recommendations')
def recommendations(lat:float,lng:float,radius_km:float=Query(25,ge=0,le=500),energy_kwh:float=Query(20,gt=0),connector_type:Optional[str]=None,conn=Depends(db)):
    rows=q(conn,"SELECT id FROM charging_stations WHERE status='active' AND deleted_at IS NULL")
    out=[]
    for row in rows:
        s=station_info(conn,row['id'],lat,lng,energy_kwh)
        if s and s['distance_km']<=radius_km and (not connector_type or s['connector_type'].lower()==connector_type.lower()):out.append(s)
    return sorted(out,key=lambda x:x['recommendation_score'],reverse=True)
@app.get('/',include_in_schema=False)
def home_page():
    page=Path(__file__).with_name('zero_latency_interactive_preview_human_copy.html')
    return FileResponse(page,media_type='text/html')

@app.get('/stations/{station_id}')
def get_station(station_id:str,conn=Depends(db)):return station_or_404(conn,station_id)
@app.post('/stations')
def create_station(data:StationIn,conn=Depends(db),user=Depends(role('host'))):
    if data.pricing_type not in ('per_kwh','per_hour'):raise HTTPException(422,'pricing_type must be per_kwh or per_hour')
    sid=uid()
    try:
        exec_sql(conn,"INSERT INTO charging_stations(id,host_id,title,address,connector_type,coordinates,pricing_type,price_rate,status) VALUES(%s,%s,%s,%s,%s,ST_GeomFromText(CONCAT('POINT(',%s,' ',%s,')'),4326,'axis-order=long-lat'),%s,%s,'active')",(sid,user['id'],data.title.strip(),data.address,data.connector_type,data.longitude,data.latitude,data.pricing_type,data.price_rate));conn.commit()
    except Exception:conn.rollback();raise
    return station_or_404(conn,sid)
@app.patch('/stations/{station_id}')
def update_station(station_id:str,data:StationUpdate,conn=Depends(db),user=Depends(role('host'))):
    if data.pricing_type not in ('per_kwh','per_hour'):raise HTTPException(422,'pricing_type must be per_kwh or per_hour')
    owned_station(conn,station_id,user)
    if data.address is not None and (data.latitude is None or data.longitude is None):raise HTTPException(422,'Provide coordinates when updating the station address')
    if (data.latitude is None)!=(data.longitude is None):raise HTTPException(422,'Latitude and longitude must be provided together')
    if data.latitude is not None:
        exec_sql(conn,"UPDATE charging_stations SET address=COALESCE(%s,address),coordinates=ST_GeomFromText(CONCAT('POINT(',%s,' ',%s,')'),4326,'axis-order=long-lat'),pricing_type=%s,price_rate=%s WHERE id=%s AND host_id=%s AND deleted_at IS NULL",(data.address.strip() if data.address is not None else None,data.longitude,data.latitude,data.pricing_type,data.price_rate,station_id,user['id']))
    else:
        exec_sql(conn,'UPDATE charging_stations SET pricing_type=%s,price_rate=%s WHERE id=%s AND host_id=%s AND deleted_at IS NULL',(data.pricing_type,data.price_rate,station_id,user['id']))
    conn.commit()
    return station_or_404(conn,station_id)
@app.patch('/stations/{station_id}/status')
def update_station_status(station_id:str,data:StationStatusIn,conn=Depends(db),user=Depends(role('host'))):
    if data.status not in ('active','inactive'):raise HTTPException(422,'status must be active or inactive')
    owned_station(conn,station_id,user)
    exec_sql(conn,'UPDATE charging_stations SET status=%s WHERE id=%s AND host_id=%s AND deleted_at IS NULL',(data.status,station_id,user['id']));conn.commit()
    return {'id':station_id,'status':data.status}
@app.delete('/stations/{station_id}')
def delete_station(station_id:str,conn=Depends(db),user=Depends(role('host'))):
    owned_station(conn,station_id,user)
    active=q(conn,"SELECT id FROM bookings WHERE station_id=%s AND status IN ('reserved','charging') LIMIT 1",(station_id,),True)
    if active:raise HTTPException(409,'Cancel or complete active bookings before deleting this station')
    exec_sql(conn,"UPDATE charging_stations SET status='inactive',deleted_at=UTC_TIMESTAMP() WHERE id=%s AND host_id=%s AND deleted_at IS NULL",(station_id,user['id']));conn.commit()
    return {'id':station_id,'deleted':True}
@app.get('/stations/{station_id}/slots')
def slots(station_id:str,available_only:bool=False,conn=Depends(db)):
    station_or_404(conn,station_id)
    sql='SELECT id,station_id,start_time,end_time,is_booked FROM time_slots WHERE station_id=%s AND start_time>UTC_TIMESTAMP()'
    if available_only:sql+=' AND is_booked=0'
    return plain(q(conn,sql+' ORDER BY start_time',(station_id,)))
@app.post('/stations/{station_id}/slots')
def add_slot(station_id:str,data:SlotIn,conn=Depends(db),user=Depends(role('host'))):
    owned_station(conn,station_id,user)
    start=slot_datetime_to_utc(data.start_time);end=slot_datetime_to_utc(data.end_time)
    if start>=end or start<dt.datetime.utcnow():raise HTTPException(422,'Slot must have a future start and end after start')
    conflict=q(conn,"SELECT id FROM time_slots WHERE station_id=%s AND start_time<%s AND end_time>%s LIMIT 1",(station_id,end,start),True)
    if conflict:raise HTTPException(409,'This availability overlaps another slot')
    sid=uid();exec_sql(conn,'INSERT INTO time_slots(id,station_id,start_time,end_time) VALUES(%s,%s,%s,%s)',(sid,station_id,start,end));conn.commit()
    return {'id':sid,'station_id':station_id,'start_time':start,'end_time':end,'is_booked':False}
@app.put('/slots/{slot_id}')
def update_slot(slot_id: str, data: SlotIn, conn=Depends(db), user=Depends(role('host'))):
    slot = q(
        conn,
        'SELECT id,station_id,is_booked FROM time_slots WHERE id=%s FOR UPDATE',
        (slot_id,),
        True,
    )
    if not slot:
        raise HTTPException(404, 'Slot not found')

    # Confirms this slot belongs to a station owned by the signed-in host.
    owned_station(conn, slot['station_id'], user)

    if slot['is_booked']:
        raise HTTPException(409, 'Booked slots cannot be changed')

    start = slot_datetime_to_utc(data.start_time)
    end = slot_datetime_to_utc(data.end_time)
    if start >= end or start < dt.datetime.utcnow():
        raise HTTPException(422, 'Slot must have a future start and end after start')

    conflict = q(
        conn,
        '''SELECT id FROM time_slots
           WHERE station_id=%s AND id<>%s
             AND start_time<%s AND end_time>%s
           LIMIT 1''',
        (slot['station_id'], slot_id, end, start),
        True,
    )
    if conflict:
        raise HTTPException(409, 'This availability overlaps another slot')

    try:
        exec_sql(
            conn,
            'UPDATE time_slots SET start_time=%s,end_time=%s WHERE id=%s',
            (start, end, slot_id),
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        if 'overlap' in str(exc).lower():
            raise HTTPException(409, 'This availability overlaps another slot')
        raise

    return {'id': slot_id, 'start_time': start, 'end_time': end}


@app.delete('/slots/{slot_id}')
def delete_slot(slot_id: str, conn=Depends(db), user=Depends(role('host'))):
    slot = q(
        conn,
        'SELECT id,station_id,is_booked FROM time_slots WHERE id=%s FOR UPDATE',
        (slot_id,),
        True,
    )
    if not slot:
        raise HTTPException(404, 'Slot not found')

    # Confirms this slot belongs to a station owned by the signed-in host.
    owned_station(conn, slot['station_id'], user)

    if slot['is_booked']:
        raise HTTPException(409, 'Booked slots cannot be deleted')

    exec_sql(conn, 'DELETE FROM time_slots WHERE id=%s', (slot_id,))
    conn.commit()
    return {'id': slot_id, 'deleted': True}
@app.get('/stations/{station_id}/quote')
def quote(station_id:str,energy_kwh:float=Query(20,gt=0),duration_minutes:int=Query(60,gt=0),conn=Depends(db)):
    s=station_or_404(conn,station_id);effective_rule(conn,station_id)
    rate=s['effective_rate'];total=rate*energy_kwh if s['pricing_type']=='per_kwh' else rate*duration_minutes/60
    return {'station_id':station_id,'base_rate':s['price_rate'],'effective_rate':rate,'pricing_type':s['pricing_type'],'estimated_total':round(total,2),'energy_kwh':energy_kwh,'duration_minutes':duration_minutes,'pricing_rule':s['pricing_rule']}
@app.get('/stations/{station_id}/pricing-rules')
def list_rules(station_id:str,conn=Depends(db)):station_or_404(conn,station_id);return plain(q(conn,'SELECT * FROM station_pricing_rules WHERE station_id=%s ORDER BY created_at DESC',(station_id,)))
@app.post('/stations/{station_id}/pricing-rules')
def add_rule(station_id:str,data:RuleIn,conn=Depends(db),user=Depends(role('host'))):
    owned_station(conn,station_id,user);rid=uid()
    exec_sql(conn,'INSERT INTO station_pricing_rules(id,station_id,label,start_time,end_time,multiplier,active) VALUES(%s,%s,%s,%s,%s,%s,%s)',(rid,station_id,data.label,data.start_time,data.end_time,data.multiplier,data.active));conn.commit()
    return plain(q(conn,'SELECT * FROM station_pricing_rules WHERE id=%s',(rid,),True))
@app.delete('/pricing-rules/{rule_id}')
def delete_rule(rule_id:str,conn=Depends(db),user=Depends(role('host'))):
    r=q(conn,'SELECT * FROM station_pricing_rules WHERE id=%s',(rule_id,),True)
    if not r:raise HTTPException(404,'Pricing rule not found')
    owned_station(conn,r['station_id'],user);exec_sql(conn,'DELETE FROM station_pricing_rules WHERE id=%s',(rule_id,));conn.commit();return {'deleted':True}
@app.get('/stations/{station_id}/reviews')
def reviews(station_id:str,conn=Depends(db)):
    station_or_404(conn,station_id);rows=q(conn,'SELECT r.id,r.rating,r.comment,r.created_at,u.name reviewer_name FROM station_reviews r JOIN users u ON u.id=r.consumer_id WHERE r.station_id=%s ORDER BY r.created_at DESC',(station_id,));ratings=[int(x['rating']) for x in rows]
    return {'summary':{'average_rating':round(sum(ratings)/len(ratings),1) if ratings else None,'review_count':len(ratings)},'reviews':plain(rows)}
@app.post('/stations/{station_id}/reviews')
def add_review(station_id:str,data:ReviewIn,conn=Depends(db),user=Depends(role('consumer'))):
    station_or_404(conn,station_id)
    done=q(conn,"SELECT id FROM bookings WHERE consumer_id=%s AND station_id=%s AND status='completed' LIMIT 1",(user['id'],station_id),True)
    if not done:raise HTTPException(403,'A completed charging session is required to review this station')
    if q(conn,'SELECT id FROM station_reviews WHERE consumer_id=%s AND station_id=%s',(user['id'],station_id),True):raise HTTPException(409,'You have already reviewed this station')
    rid=uid();exec_sql(conn,'INSERT INTO station_reviews(id,station_id,consumer_id,rating,comment) VALUES(%s,%s,%s,%s,%s)',(rid,station_id,user['id'],data.rating,data.comment));conn.commit();return {'id':rid}
@app.get('/stations/{station_id}/reliability')
def reliability(station_id:str,conn=Depends(db)):
    s=station_or_404(conn,station_id);r=q(conn,"SELECT COUNT(*) reports FROM station_reports WHERE station_id=%s AND status IN ('open','investigating')",(station_id,),True)
    return {'reliability_label':'Needs attention' if r['reports']>=3 else s['reliability_status'],'open_reports':r['reports'],'average_rating':s['average_rating'],'review_count':s['review_count'],'note':'Multiple recent issue reports have been submitted.' if r['reports']>=3 else 'Check recent feedback and confirm availability before travel.'}
@app.post('/stations/{station_id}/reports')
def add_report(station_id:str,data:ReportIn,conn=Depends(db),user=Depends(current_user)):
    station_or_404(conn,station_id);allowed=('not_working','access_issue','safety','incorrect_listing','other')
    if data.category not in allowed:raise HTTPException(422,'Invalid report category')
    rid=uid();exec_sql(conn,'INSERT INTO station_reports(id,station_id,reporter_id,category,description) VALUES(%s,%s,%s,%s,%s)',(rid,station_id,user['id'],data.category,data.description));conn.commit();return {'id':rid,'status':'open'}

@app.post('/bookings')
def create_booking(body:BookingCreate,conn=Depends(db),user=Depends(role('consumer'))):
    slot_id=body.slot_id
    phone=normalize_phone(body.phone_number)
    vehicle=normalize_vehicle_registration(body.vehicle_registration)
    now=dt.datetime.utcnow()
    begin(conn)
    try:
        slot=q(conn,'SELECT * FROM time_slots WHERE id=%s FOR UPDATE',(slot_id,),True)
        if not slot:raise HTTPException(404,'Slot not found')
        station=q(conn,'SELECT status FROM charging_stations WHERE id=%s AND deleted_at IS NULL',(slot['station_id'],),True)
        if not station or station['status']!='active':raise HTTPException(409,'This station is not active')
        if slot['is_booked']:
            existing=q(conn,"""SELECT b.id,b.status,v.phone_number,v.vehicle_registration,v.otp_delivery_status
                FROM bookings b JOIN booking_consumer_verifications v ON v.booking_id=b.id
                WHERE b.slot_id=%s AND b.consumer_id=%s AND b.status='reserved' FOR UPDATE""",(slot_id,user['id']),True)
            if existing and existing['phone_number']==phone and existing['vehicle_registration']==vehicle:
                conn.commit()
                return {'id':existing['id'],'status':existing['status'],'session_status':'otp_pending','otp_delivery_status':existing['otp_delivery_status'],'already_created':True}
            raise HTTPException(409,'Slot is no longer available')
        if slot['start_time']<now:raise HTTPException(409,'Slot is no longer available')
        reserve_otp_send(conn,user['id'],now)
        sid=slot['station_id'];bid=uid();code,code_hash=new_otp(bid)
        exec_sql(conn,"INSERT INTO bookings(id,consumer_id,station_id,slot_id,status) VALUES(%s,%s,%s,%s,'reserved')",(bid,user['id'],sid,slot_id))
        exec_sql(conn,"""INSERT INTO booking_consumer_verifications
            (booking_id,consumer_id,phone_number,vehicle_registration,otp_hash,otp_expires_at,otp_last_sent_at,otp_window_started_at,otp_send_count,otp_delivery_status)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,1,'pending')""",
            (bid,user['id'],phone,vehicle,code_hash,now+dt.timedelta(minutes=10),now,now))
        conn.commit()
    except Exception:conn.rollback();raise
    delivery_status=demo_otp_delivery_status()
    save_otp_delivery_status(conn,bid,delivery_status)
    response={'id':bid,'status':'reserved','session_status':'otp_pending','otp_delivery_status':delivery_status,'otp_resend_after_seconds':OTP_RESEND_COOLDOWN_SECONDS}
    response['demo_otp']=code
    response['message']='Booking confirmed. DEMO OTP — no SMS sent. Show this code to your charging host.'
    return response
@app.get('/bookings/mine')
def my_bookings(conn=Depends(db),user=Depends(current_user)):
    if user['role']=='consumer':
        bookings=q(conn,"""SELECT b.id,b.station_id,b.status,b.created_at,b.started_at,b.ended_at,b.energy_consumed,
            b.pricing_type_at_start,b.base_rate_at_start,b.pricing_multiplier_at_start,
            CASE WHEN b.started_at IS NOT NULL THEN TIMESTAMPDIFF(SECOND,b.started_at,COALESCE(b.ended_at,UTC_TIMESTAMP())) ELSE NULL END charged_duration_seconds,
            tx.total_amount,si.booking_id IS NOT NULL session_interrupted,
            s.title station_title,t.start_time slot_start,t.end_time slot_end,
            CASE WHEN b.status='charging' THEN 'session_active' WHEN b.status='completed' THEN 'session_completed' WHEN b.status='cancelled' THEN 'booking_cancelled' WHEN v.otp_verified_at IS NOT NULL THEN 'awaiting_host_start' WHEN v.booking_id IS NOT NULL THEN 'otp_pending' ELSE 'booking_confirmed' END session_status,
            CASE WHEN v.booking_id IS NULL THEN 'needs_details' WHEN v.otp_used_at IS NOT NULL THEN 'used' WHEN v.otp_expires_at<=UTC_TIMESTAMP() THEN 'expired' ELSE v.otp_delivery_status END otp_delivery_status,
            CASE WHEN b.status='reserved' AND v.otp_used_at IS NULL THEN GREATEST(0,CEIL(TIMESTAMPDIFF(MICROSECOND,UTC_TIMESTAMP(6),DATE_ADD(v.otp_last_sent_at,INTERVAL 180 SECOND))/1000000)) ELSE 0 END otp_resend_after_seconds
            FROM bookings b JOIN charging_stations s ON s.id=b.station_id JOIN time_slots t ON t.id=b.slot_id
            LEFT JOIN booking_consumer_verifications v ON v.booking_id=b.id
            LEFT JOIN transactions tx ON tx.booking_id=b.id AND tx.status='completed'
            LEFT JOIN session_interruptions si ON si.booking_id=b.id
            WHERE b.consumer_id=%s ORDER BY b.created_at DESC""",(user['id'],))
        now=dt.datetime.utcnow()
        for booking in bookings:
            booking['estimated_amount']=None
            booking['estimated_energy_kwh']=None
            if booking['status']=='charging' and booking['started_at']:
                elapsed=max(0,(now-booking['started_at']).total_seconds())
                energy=min(elapsed/3600*7.2,40)
                pricing_type,base_rate,multiplier,_=session_pricing(conn,booking)
                rate=base_rate*multiplier
                cost=energy*rate if pricing_type=='per_kwh' else elapsed/3600*rate
                booking['estimated_amount']=round(cost,2)
                booking['estimated_energy_kwh']=round(energy,2)
        return plain(bookings)
    return plain(q(conn,"""SELECT b.id,b.station_id,b.status,b.started_at,b.ended_at,b.energy_consumed,s.title station_title,t.start_time slot_start,t.end_time slot_end
        FROM bookings b JOIN charging_stations s ON s.id=b.station_id JOIN time_slots t ON t.id=b.slot_id
        WHERE b.consumer_id=%s ORDER BY b.created_at DESC""",(user['id'],)))
@app.post('/bookings/{booking_id}/otp/resend')
def resend_booking_otp(booking_id:str,conn=Depends(db),user=Depends(role('consumer'))):
    begin(conn)
    try:
        booking=owned_booking(conn,booking_id,user,True)
        if booking['status']!='reserved':raise HTTPException(409,'A code can be resent only for a reserved booking')
        verification=q(conn,'SELECT * FROM booking_consumer_verifications WHERE booking_id=%s FOR UPDATE',(booking_id,),True)
        if not verification:raise HTTPException(409,'This booking predates phone verification. Cancel it and make a new booking to add your phone and vehicle details.')
        now=dt.datetime.utcnow()
        slot=q(conn,'SELECT end_time FROM time_slots WHERE id=%s',(booking['slot_id'],),True)
        if not slot or slot['end_time']<=now:
            exec_sql(conn,"UPDATE bookings SET status='no_show' WHERE id=%s",(booking_id,))
            exec_sql(conn,"UPDATE booking_consumer_verifications SET otp_delivery_status='expired' WHERE booking_id=%s",(booking_id,))
            conn.commit()
            raise HTTPException(410,'This booking has expired and can no longer be started')
        if verification['otp_window_started_at']<now-dt.timedelta(hours=1):
            window_start=now;send_count=0
        else:
            window_start=verification['otp_window_started_at'];send_count=int(verification['otp_send_count'])
        if verification['otp_used_at'] is not None:raise HTTPException(409,'This verification code has already been used')
        cooldown_remaining=otp_resend_seconds(verification['otp_last_sent_at'],now)
        if cooldown_remaining>0:
            raise HTTPException(429,f'Please wait {cooldown_remaining} seconds before requesting another code')
        if send_count>=4:raise HTTPException(429,'This booking has reached its hourly code limit. Try again later.')
        reserve_otp_send(conn,user['id'],now)
        code,code_hash=new_otp(booking_id)
        exec_sql(conn,"""UPDATE booking_consumer_verifications SET otp_hash=%s,otp_expires_at=%s,otp_used_at=NULL,otp_verified_at=NULL,
            otp_attempts=0,otp_last_sent_at=%s,otp_window_started_at=%s,otp_send_count=%s,otp_delivery_status='pending'
            WHERE booking_id=%s""",(code_hash,now+dt.timedelta(minutes=10),now,window_start,send_count+1,booking_id))
        conn.commit()
    except Exception:conn.rollback();raise
    delivery_status=demo_otp_delivery_status()
    save_otp_delivery_status(conn,booking_id,delivery_status)
    return {'booking_id':booking_id,'status':'reserved','session_status':'otp_pending','otp_delivery_status':delivery_status,'otp_resend_after_seconds':OTP_RESEND_COOLDOWN_SECONDS,'demo_otp':code,'message':'New DEMO OTP generated — no SMS sent. Show this code to your charging host.'}
@app.post('/bookings/{booking_id}/strike')
def strike_consumer(booking_id:str,data:StrikeIn,conn=Depends(db),user=Depends(role('host'))):
    begin(conn)
    try:
        booking=q(conn,"SELECT b.id,b.consumer_id FROM bookings b JOIN charging_stations s ON s.id=b.station_id WHERE b.id=%s AND s.host_id=%s AND s.deleted_at IS NULL FOR UPDATE",(booking_id,user['id']),True)
        if not booking:raise HTTPException(404,'Booking not found for one of your stations')
        consumer=q(conn,'SELECT id,banned_at FROM users WHERE id=%s FOR UPDATE',(booking['consumer_id'],),True)
        if consumer['banned_at']:raise HTTPException(409,'This consumer is already banned')
        if q(conn,'SELECT id FROM consumer_strikes WHERE booking_id=%s',(booking_id,),True):raise HTTPException(409,'A strike has already been issued for this booking')
        exec_sql(conn,'INSERT INTO consumer_strikes(id,consumer_id,host_id,booking_id,reason) VALUES(%s,%s,%s,%s,%s)',(uid(),booking['consumer_id'],user['id'],booking_id,data.reason.strip()))
        result=q(conn,'SELECT COUNT(*) strike_count FROM consumer_strikes WHERE consumer_id=%s',(booking['consumer_id'],),True)
        strike_count=int(result['strike_count']);banned=strike_count>=3
        if banned:exec_sql(conn,'UPDATE users SET banned_at=COALESCE(banned_at,UTC_TIMESTAMP()) WHERE id=%s',(booking['consumer_id'],))
        conn.commit()
        return {'booking_id':booking_id,'consumer_id':booking['consumer_id'],'strike_count':strike_count,'banned':banned}
    except Exception:conn.rollback();raise
@app.get('/consumer/strikes/mine')
def consumer_strikes_mine(conn=Depends(db),user=Depends(role('consumer'))):
    strikes=q(conn,"""SELECT cs.id,cs.reason,cs.created_at,cs.booking_id,s.title station_title
        FROM consumer_strikes cs JOIN bookings b ON b.id=cs.booking_id
        JOIN charging_stations s ON s.id=b.station_id
        WHERE cs.consumer_id=%s ORDER BY cs.created_at DESC""",(user['id'],))
    return {'strike_count':len(strikes),'strikes':plain(strikes)}
def owned_booking(conn,bid,user,lock=False):
    b=q(conn,'SELECT * FROM bookings WHERE id=%s'+(' FOR UPDATE' if lock else ''),(bid,),True)
    if not b:raise HTTPException(404,'Booking not found')
    if b['consumer_id']!=user['id']:raise HTTPException(403,'This booking belongs to another account')
    return b

def session_pricing(conn,booking):
    pricing_type=booking.get('pricing_type_at_start')
    base_rate=booking.get('base_rate_at_start')
    multiplier=booking.get('pricing_multiplier_at_start')
    label=booking.get('pricing_rule_at_start')
    if pricing_type is not None and base_rate is not None and multiplier is not None:
        return str(pricing_type),float(base_rate),float(multiplier),label
    station=q(conn,'SELECT pricing_type,price_rate FROM charging_stations WHERE id=%s',(booking['station_id'],),True)
    if not station:raise HTTPException(404,'Station not found')
    rule=effective_rule(conn,booking['station_id'])
    return station['pricing_type'],float(station['price_rate']),float(rule['multiplier']) if rule else 1.0,rule['label'] if rule else None

def finalize_session(conn,booking,energy_kwh,now=None):
    now=now or dt.datetime.utcnow()
    elapsed=max(0,(now-booking['started_at']).total_seconds())
    pricing_type,base_rate,multiplier,label=session_pricing(conn,booking)
    effective_rate=base_rate*multiplier
    total=energy_kwh*effective_rate if pricing_type=='per_kwh' else elapsed/3600*effective_rate
    total=round(max(0,total),2)
    fee=round(total*.10,2)
    payout=round(total-fee,2)
    exec_sql(conn,"""UPDATE bookings SET status='completed',ended_at=%s,energy_consumed=%s,
        pricing_type_at_start=%s,base_rate_at_start=%s,pricing_multiplier_at_start=%s,
        pricing_rule_at_start=%s WHERE id=%s""",
        (now,energy_kwh,pricing_type,base_rate,multiplier,label,booking['id']))
    exec_sql(conn,"INSERT INTO transactions(id,booking_id,total_amount,platform_fee,host_payout,status) VALUES(%s,%s,%s,%s,%s,'completed')",(uid(),booking['id'],total,fee,payout))
    return {'energy_kwh':energy_kwh,'total_amount':total,'platform_fee':fee,'host_payout':payout,'ended_at':now}

@app.get('/notifications/mine')
def notifications_mine(conn=Depends(db),user=Depends(current_user)):
    return plain(q(conn,"SELECT id,notification_type,title,message,booking_id,created_at FROM user_notifications WHERE user_id=%s ORDER BY created_at DESC LIMIT 40",(user['id'],)))
@app.post('/bookings/{booking_id}/otp/verify')
def verify_booking_otp(booking_id:str,data:VerifyBookingOtpIn,conn=Depends(db),user=Depends(role('host'))):
    begin(conn)
    try:
        booking=q(conn,"""SELECT b.id,b.status,b.station_id,sl.start_time,sl.end_time,s.host_id
            FROM bookings b JOIN charging_stations s ON s.id=b.station_id
            JOIN time_slots sl ON sl.id=b.slot_id WHERE b.id=%s FOR UPDATE""",(booking_id,),True)
        if not booking:raise HTTPException(404,'Booking not found')
        if booking['host_id']!=user['id']:raise HTTPException(404,'Booking not found')
        if booking['status']!='reserved':raise HTTPException(409,'Only reserved bookings can be started')
        now=dt.datetime.utcnow()
        if booking['end_time']<=now:
            exec_sql(conn,"UPDATE bookings SET status='no_show' WHERE id=%s",(booking_id,))
            exec_sql(conn,"UPDATE booking_consumer_verifications SET otp_delivery_status='expired' WHERE booking_id=%s",(booking_id,))
            conn.commit()
            raise HTTPException(410,'This booking has expired and cannot be started')
        if booking['start_time']>now:raise HTTPException(409,'This booking has not started yet')
        verification=q(conn,'SELECT * FROM booking_consumer_verifications WHERE booking_id=%s FOR UPDATE',(booking_id,),True)
        if not verification:raise HTTPException(409,'This booking has no consumer verification code')
        if verification['otp_verified_at'] is not None or verification['otp_used_at'] is not None:raise HTTPException(409,'This verification code has already been used')
        if verification['otp_expires_at']<=now:
            exec_sql(conn,"UPDATE booking_consumer_verifications SET otp_delivery_status='expired' WHERE booking_id=%s",(booking_id,))
            conn.commit()
            raise HTTPException(410,'The verification code has expired. Ask the consumer to resend it.')
        if int(verification['otp_attempts'])>=5:raise HTTPException(429,'Too many incorrect codes. Ask the consumer to request a new code.')
        if not otp_matches(booking_id,data.otp_code,verification['otp_hash']):
            attempts=int(verification['otp_attempts'])+1
            exec_sql(conn,'UPDATE booking_consumer_verifications SET otp_attempts=%s WHERE booking_id=%s',(attempts,booking_id))
            conn.commit()
            if attempts>=5:raise HTTPException(429,'Too many incorrect codes. Ask the consumer to request a new code.')
            raise HTTPException(400,f'Incorrect verification code. {5-attempts} attempts remaining.')
        exec_sql(conn,"UPDATE booking_consumer_verifications SET otp_used_at=%s,otp_verified_at=%s,otp_delivery_status='used' WHERE booking_id=%s",(now,now,booking_id))
        conn.commit()
        return {'id':booking_id,'status':'reserved','session_status':'awaiting_host_start','otp_verified_at':now}
    except Exception:conn.rollback();raise
@app.post('/bookings/{booking_id}/start')
def start(booking_id:str,conn=Depends(db),user=Depends(current_user)):
    begin(conn)
    try:
        booking=q(conn,"""SELECT b.id,b.consumer_id,b.status,b.station_id,sl.start_time,sl.end_time,s.host_id,s.pricing_type,s.price_rate
            FROM bookings b JOIN charging_stations s ON s.id=b.station_id
            JOIN time_slots sl ON sl.id=b.slot_id WHERE b.id=%s FOR UPDATE""",(booking_id,),True)
        if not booking:raise HTTPException(404,'Booking not found')
        if user['role']=='host':
            if booking['host_id']!=user['id']:raise HTTPException(404,'Booking not found')
        elif user['role']=='consumer':
            if booking['consumer_id']!=user['id']:raise HTTPException(404,'Booking not found')
        else:
            raise HTTPException(403,'Consumer or assigned host account required')
        if booking['status']!='reserved':raise HTTPException(409,'Only reserved bookings can be started')
        now=dt.datetime.utcnow()
        if booking['end_time']<=now:
            exec_sql(conn,"UPDATE bookings SET status='no_show' WHERE id=%s",(booking_id,))
            exec_sql(conn,"UPDATE booking_consumer_verifications SET otp_delivery_status='expired' WHERE booking_id=%s",(booking_id,))
            conn.commit()
            raise HTTPException(410,'This booking has expired and cannot be started')
        if booking['start_time']>now:raise HTTPException(409,'This booking has not started yet')
        verification=q(conn,'SELECT otp_verified_at FROM booking_consumer_verifications WHERE booking_id=%s FOR UPDATE',(booking_id,),True)
        if not verification or verification['otp_verified_at'] is None:raise HTTPException(409,'Verify the consumer OTP before starting this session')
        rule=effective_rule(conn,booking['station_id'],lock=True)
        exec_sql(conn,"""UPDATE bookings SET status='charging',started_at=%s,pricing_type_at_start=%s,
            base_rate_at_start=%s,pricing_multiplier_at_start=%s,pricing_rule_at_start=%s WHERE id=%s""",
            (now,booking['pricing_type'],booking['price_rate'],float(rule['multiplier']) if rule else 1.0,rule['label'] if rule else None,booking_id))
        conn.commit()
        return {'id':booking_id,'status':'charging','session_status':'session_active','started_at':now,'station_id':booking['station_id']}
    except Exception:conn.rollback();raise
@app.post('/bookings/{booking_id}/cancel')
def cancel(booking_id:str,conn=Depends(db),user=Depends(role('consumer'))):
    begin(conn)
    try:
        b=owned_booking(conn,booking_id,user,True)
        if b['status']!='reserved':raise HTTPException(409,'Only reserved bookings can be cancelled')
        exec_sql(conn,"UPDATE bookings SET status='cancelled' WHERE id=%s",(booking_id,));exec_sql(conn,"UPDATE booking_consumer_verifications SET otp_delivery_status='expired' WHERE booking_id=%s",(booking_id,));exec_sql(conn,'UPDATE time_slots SET is_booked=0 WHERE id=%s',(b['slot_id'],));conn.commit();return {'id':booking_id,'status':'cancelled'}
    except Exception:conn.rollback();raise
@app.get('/sessions/{booking_id}/live')
def live(booking_id:str,conn=Depends(db),user=Depends(current_user)):
    b=owned_booking(conn,booking_id,user)
    if b['status']!='charging':raise HTTPException(409,'This booking has no live session')
    st=station_or_404(conn,b['station_id']);elapsed=max(0,(dt.datetime.utcnow()-b['started_at']).total_seconds());energy=min(elapsed/3600*7.2,40);pricing_type,base_rate,multiplier,label=session_pricing(conn,b);rate=base_rate*multiplier
    cost=energy*rate if pricing_type=='per_kwh' else elapsed/3600*rate
    return {'booking_id':booking_id,'station_title':st['title'],'elapsed_seconds':int(elapsed),'estimated_energy_kwh':round(energy,2),'estimated_cost':round(cost,2),'pricing_rule':label or 'Session start rate','telemetry':'estimated'}
@app.post('/bookings/{booking_id}/stop')
def stop(booking_id:str,data:StopIn,conn=Depends(db),user=Depends(role('consumer'))):
    begin(conn)
    try:  
        b=owned_booking(conn,booking_id,user,True)
        if b['status']!='charging':raise HTTPException(409,'Only active sessions can be stopped')
        result=finalize_session(conn,b,data.energy_kwh);conn.commit()
        return {'id':booking_id,'status':'completed','energy_kwh':result['energy_kwh'],'total_amount':result['total_amount'],'platform_fee':result['platform_fee'],'host_payout':result['host_payout']}
    except Exception:conn.rollback();raise
@app.post('/bookings/{booking_id}/interrupt')
def interrupt_session(booking_id:str,data:SessionInterruptionIn,conn=Depends(db),user=Depends(role('host'))):
    reason=data.reason.strip()
    if reason not in ('Power cut','Charger fault','Other'):
        raise HTTPException(422,'Choose Power cut, Charger fault, or Other')
    description=(data.description or '').strip()
    begin(conn)
    try:
        booking=q(conn,"""SELECT b.*,s.host_id,s.title station_title
            FROM bookings b JOIN charging_stations s ON s.id=b.station_id
            WHERE b.id=%s FOR UPDATE""",(booking_id,),True)
        if not booking or booking['host_id']!=user['id']:raise HTTPException(404,'Booking not found')
        if booking['status']!='charging':raise HTTPException(409,'Only active sessions can be interrupted')
        now=dt.datetime.utcnow()
        elapsed=max(0,(now-booking['started_at']).total_seconds())
        energy=round(min(elapsed/3600*7.2,40),2)
        result=finalize_session(conn,booking,energy,now)
        interruption_id=uid()
        exec_sql(conn,"""INSERT INTO session_interruptions
            (id,booking_id,host_id,reason,description,estimated_energy_kwh,estimated_amount)
            VALUES(%s,%s,%s,%s,%s,%s,%s)""",
            (interruption_id,booking_id,user['id'],reason,description,energy,result['total_amount']))
        consumer_message=(f"Charging at {booking['station_title']} stopped unexpectedly. "
            f"Estimated energy delivered: {energy:.2f} kWh. Final estimated amount: ₹{result['total_amount']:.2f}.")
        host_message=(f"{reason} reported for booking {booking_id}. "
            f"Session ended with an estimated {energy:.2f} kWh and final amount ₹{result['total_amount']:.2f}.")
        if description:host_message+=f" Details: {description}"
        for recipient_id,title,message in (
            (booking['consumer_id'],'Charging session interrupted',consumer_message),
            (user['id'],'Interruption report recorded',host_message),
        ):
            exec_sql(conn,"""INSERT INTO user_notifications
                (id,user_id,notification_type,title,message,booking_id)
                VALUES(%s,%s,'session_interruption',%s,%s,%s)""",
                (uid(),recipient_id,title,message,booking_id))
        conn.commit()
        return {'id':booking_id,'status':'completed','interruption_id':interruption_id,'reason':reason,
            'description':description,'energy_kwh':energy,'estimated':True,'total_amount':result['total_amount'],
            'platform_fee':result['platform_fee'],'host_payout':result['host_payout'],'ended_at':now}
    except Exception:conn.rollback();raise
@app.get('/bookings/{booking_id}/payment')
def booking_payment(booking_id:str,conn=Depends(db),user=Depends(role('consumer'))):
    booking=owned_booking(conn,booking_id,user)
    if booking['status']!='completed':raise HTTPException(409,'Payment is available only after charging is complete')
    payment=q(conn,"""SELECT b.id booking_id,b.created_at,b.started_at,b.ended_at,b.energy_consumed energy_kwh,
        s.title station_title,s.address station_location,s.connector_type,
        COALESCE(b.pricing_type_at_start,s.pricing_type) pricing_type,
        COALESCE(b.base_rate_at_start,s.price_rate) price_rate,
        COALESCE(b.pricing_multiplier_at_start,1) pricing_multiplier,b.pricing_rule_at_start,
        EXISTS(SELECT 1 FROM session_interruptions si WHERE si.booking_id=b.id) session_interrupted,
        sl.start_time slot_start,sl.end_time slot_end,tx.total_amount,tx.platform_fee
        FROM bookings b JOIN charging_stations s ON s.id=b.station_id
        JOIN time_slots sl ON sl.id=b.slot_id
        JOIN transactions tx ON tx.booking_id=b.id AND tx.status='completed'
        WHERE b.id=%s AND b.consumer_id=%s""",(booking_id,user['id']),True)
    if not payment:raise HTTPException(404,'Completed booking payment details not found')
    return plain(payment)
@app.get('/impact/mine')
def impact(conn=Depends(db),user=Depends(current_user)):
    s=q(conn,"SELECT COALESCE(SUM(energy_consumed),0) energy_kwh,COUNT(*) completed_sessions FROM bookings WHERE consumer_id=%s AND status='completed'",(user['id'],),True)
    return {'energy_kwh':float(s['energy_kwh']),'completed_sessions':s['completed_sessions'],'estimated_co2_avoided_kg':round(float(s['energy_kwh'])*.12,1)}
@app.get('/analytics/host')
def analytics(conn=Depends(db),user=Depends(role('host'))):
    summary=q(conn,"""SELECT COALESCE(SUM(t.total_amount),0) gross_revenue,COALESCE(SUM(t.platform_fee),0) platform_fees,COALESCE(SUM(t.host_payout),0) host_earnings,COUNT(DISTINCT b.id) completed_sessions,COALESCE(SUM(b.energy_consumed),0) energy_kwh FROM charging_stations s LEFT JOIN bookings b ON b.station_id=s.id AND b.status='completed' LEFT JOIN transactions t ON t.booking_id=b.id AND t.status='completed' WHERE s.host_id=%s""",(user['id'],),True)
    stations=q(conn,"""SELECT s.id,s.title,s.address,s.connector_type,s.status,s.pricing_type,s.price_rate,COALESCE(SUM(b.energy_consumed),0) energy_kwh,COUNT(DISTINCT b.id) completed_sessions,COALESCE(SUM(t.host_payout),0) host_earnings FROM charging_stations s LEFT JOIN bookings b ON b.station_id=s.id AND b.status='completed' LEFT JOIN transactions t ON t.booking_id=b.id AND t.status='completed' WHERE s.host_id=%s AND s.deleted_at IS NULL GROUP BY s.id,s.title,s.address,s.connector_type,s.status,s.pricing_type,s.price_rate ORDER BY s.created_at DESC""",(user['id'],))
    slots=q(conn,"""SELECT t.station_id,t.id,t.start_time,t.end_time,t.is_booked
        FROM time_slots t JOIN charging_stations s ON s.id=t.station_id
        WHERE s.host_id=%s AND s.deleted_at IS NULL AND t.start_time>UTC_TIMESTAMP()
        ORDER BY t.start_time""",(user['id'],))
    slots_by_station={}
    for slot in slots:
        slots_by_station.setdefault(slot['station_id'],[]).append(slot)
    for station in stations:
        station['slots']=slots_by_station.get(station['id'],[])
    bookings=q(conn,"""SELECT b.id,b.consumer_id,b.status,b.created_at,b.started_at,b.ended_at,b.energy_consumed,s.title station_title,u.name consumer_name,
        v.phone_number consumer_phone,v.vehicle_registration,
        EXISTS(SELECT 1 FROM booking_consumer_verifications cv WHERE cv.booking_id=b.id) verification_ready,
        EXISTS(SELECT 1 FROM booking_consumer_verifications cv WHERE cv.booking_id=b.id AND cv.otp_verified_at IS NOT NULL) otp_verified,
        (SELECT COUNT(*) FROM consumer_strikes cs WHERE cs.consumer_id=b.consumer_id) consumer_strikes,
        EXISTS(SELECT 1 FROM consumer_strikes cs WHERE cs.booking_id=b.id) strike_issued
        FROM bookings b JOIN charging_stations s ON s.id=b.station_id JOIN users u ON u.id=b.consumer_id
        LEFT JOIN booking_consumer_verifications v ON v.booking_id=b.id
        WHERE s.host_id=%s AND s.deleted_at IS NULL ORDER BY b.created_at DESC LIMIT 30""",(user['id'],))
    reviews=q(conn,"""SELECT r.id,r.rating,r.comment,r.created_at,s.title station_title,u.name reviewer_name
        FROM station_reviews r JOIN charging_stations s ON s.id=r.station_id
        JOIN users u ON u.id=r.consumer_id
        WHERE s.host_id=%s AND s.deleted_at IS NULL ORDER BY r.created_at DESC""",(user['id'],))
    strikes=q(conn,"""SELECT cs.id,cs.reason,cs.created_at,cs.booking_id,cs.consumer_id,
        s.title station_title,u.name consumer_name,
        (SELECT COUNT(*) FROM consumer_strikes all_strikes WHERE all_strikes.consumer_id=cs.consumer_id) strike_count
        FROM consumer_strikes cs JOIN bookings b ON b.id=cs.booking_id
        JOIN charging_stations s ON s.id=b.station_id JOIN users u ON u.id=cs.consumer_id
        WHERE cs.host_id=%s AND s.host_id=%s AND s.deleted_at IS NULL ORDER BY cs.created_at DESC""",(user['id'],user['id']))
    earnings_rows=q(conn,"""SELECT DATE(b.ended_at) day,SUM(t.host_payout) host_earnings,COUNT(*) transaction_count
        FROM transactions t JOIN bookings b ON b.id=t.booking_id
        JOIN charging_stations s ON s.id=b.station_id
        WHERE s.host_id=%s AND s.deleted_at IS NULL AND b.status='completed' AND t.status='completed'
        AND b.ended_at>=DATE_SUB(UTC_DATE(),INTERVAL 13 DAY)
        GROUP BY DATE(b.ended_at) ORDER BY DATE(b.ended_at)""",(user['id'],))
    earnings_by_day={row['day']:row for row in earnings_rows}
    today=dt.datetime.utcnow().date()
    earnings_daily=[]
    for offset in range(13,-1,-1):
        day=today-dt.timedelta(days=offset)
        row=earnings_by_day.get(day,{})
        earnings_daily.append({'day':day.isoformat(),'host_earnings':float(row.get('host_earnings') or 0),'transaction_count':int(row.get('transaction_count') or 0)})
    previous_week,current_week=earnings_daily[:7],earnings_daily[7:]
    earnings_trend=None
    if sum(day['transaction_count'] for day in previous_week)>0 and sum(day['transaction_count'] for day in current_week)>0:
        previous_total=sum(day['host_earnings'] for day in previous_week)
        current_total=sum(day['host_earnings'] for day in current_week)
        earnings_trend={'previous_7_days':round(previous_total,2),'current_7_days':round(current_total,2),'direction':'up' if current_total>previous_total else 'down' if current_total<previous_total else 'flat','change_percent':round((current_total-previous_total)/previous_total*100,1) if previous_total else None}
    return {'summary':plain(summary),'stations':plain(stations),'bookings':plain(bookings),'reviews':plain(reviews),'strikes':plain(strikes),'earnings_daily':earnings_daily,'earnings_trend':earnings_trend}
@app.post('/assistant/charge')
def assistant(data:AssistantIn,conn=Depends(db)):
    if data.latitude is None or data.longitude is None:return {'answer':'Enter your location in the station search to get nearby recommendations.','stations':[]}
    all_=recommendations(data.latitude,data.longitude,100,data.energy_kwh,data.connector_type,conn)
    all_=all_[:3]
    return {'answer':f"I found {len(all_)} nearby option{'s' if len(all_)!=1 else ''}, ranked by distance, ratings and availability.",'stations':all_}

@app.exception_handler(mysql.connector.Error)
async def mysql_error_handler(request, exc):
    print("MYSQL ERROR:", repr(exc))
    return JSONResponse(status_code=503,content={'detail':'Database operation failed. Check the MySQL connection and schema.'})