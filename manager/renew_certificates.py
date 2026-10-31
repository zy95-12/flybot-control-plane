#!/usr/bin/env python3
"""Daily private-leaf renewal with journalled rollback and scoped activation."""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import time
from operations import paused, CONFIG, stamp
from worker import Worker, ManagerError, PROJECT, atomic, run

RENEW_BEFORE = 30*86400


def fingerprint(cert):
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()


def validate(directory, hostname, ca):
    run(['openssl','verify','-CAfile',str(ca/'cert.pem'),'-verify_hostname',hostname,str(directory/'cert.pem')])
    if run(['openssl','x509','-in',str(directory/'cert.pem'),'-noout','-pubkey']) != run(['openssl','pkey','-in',str(directory/'key.pem'),'-pubout']):
        raise ManagerError('certificate_key_mismatch')
    run(['openssl','x509','-checkend',str(80*86400),'-noout','-in',str(directory/'cert.pem')])


def write_bound(path, content):
    # Individual bind mounts pin the inode. Replacing this path with rename
    # would leave the running container on the previous inode.
    if path.is_symlink() or not path.is_file(): raise ManagerError('certificate_path_conflict')
    before = path.stat().st_ino
    with path.open('r+b') as stream:
        stream.write(content); stream.truncate(); stream.flush(); os.fsync(stream.fileno())
    if path.stat().st_ino != before: raise ManagerError('certificate_inode_changed')


def probe(worker, target, expected, timeout=120):
    context = ssl.create_default_context(cafile=str(worker.ca/'cert.pem'))
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        try:
            row = worker.assert_owned(worker.inspect('container',target['container']),target.get('instance_id'))
            if target['kind']=='social':
                address='172.30.20.3';port=443
            else:
                if row['State'].get('Health',{}).get('Status')!='healthy':
                    time.sleep(2);continue
                address=next(iter(row['NetworkSettings']['Networks'].values()))['IPAddress'];port=3080
                # Tenant bridges deliberately reject traffic to the host,
                # including replies to a host-originated probe. Verify the
                # real TLS listener from its namespace without weakening that.
                code=("const tls=require('tls'),fs=require('fs');const s=tls.connect({host:"
                      +json.dumps(address)+",port:3080,servername:"+json.dumps(target['hostname'])
                      +",ca:fs.readFileSync('/run/secrets/runtime-ca.pem'),rejectUnauthorized:true},()=>{process.stdout.write(s.getPeerCertificate().fingerprint256.replace(/:/g,'').toLowerCase());s.end();});s.setTimeout(4000,()=>{s.destroy();process.exit(1)});s.on('error',()=>process.exit(1));")
                actual=run(['docker','exec',target['container'],'node','-e',code],timeout=10).strip()
                if actual==expected:return
                time.sleep(2);continue
            with socket.create_connection((address,port),4) as raw:
                with context.wrap_socket(raw,server_hostname=target['hostname']) as connection:
                    if hashlib.sha256(connection.getpeercert(binary_form=True)).hexdigest()==expected:
                        return
        except (OSError,ValueError,ManagerError): pass
        time.sleep(2)
    raise ManagerError('renewal_tls_readiness_timeout')


def activate(worker,target,directory):
    source=Path(target['directory'])
    if target['kind']=='social':
        worker.assert_owned(worker.inspect('container',worker.ingress))
        # Publish immutable complete pairs inside an already mounted directory.
        # One atomic config swap selects both files; existing TLS stays live.
        parent=worker.route_dir/'.tls';parent.mkdir(mode=0o700,exist_ok=True);os.chown(parent,10001,10001)
        generation=parent/(directory.parent.name+'-'+directory.name)
        generation.mkdir(mode=0o700,exist_ok=True);os.chown(generation,10001,10001)
        for name in ['cert.pem','key.pem']:atomic(generation/name,(directory/name).read_text(),uid=10001)
        services=worker.route_dir/'services.yml';config=json.loads(services.read_text())
        if len(config['tls']['certificates'])!=1:raise ManagerError('social_certificate_config_conflict')
        prefix='/etc/traefik/dynamic/.tls/'+generation.name+'/'
        config['tls']['certificates']=[{'certFile':prefix+'cert.pem','keyFile':prefix+'key.pem'}]
        atomic(services,json.dumps(config,indent=2),uid=10001)
        return
    if target['kind']!='social':
        row=worker.assert_owned(worker.inspect('container',target['container']),target.get('instance_id'))
        if row['State']['Running']:run(['docker','stop',target['container']],timeout=45)
    for name in ['cert.pem','key.pem']:write_bound(source/name,(directory/name).read_bytes())
    if target['was_running']:run(['docker','start',target['container']])


def mirror_social(worker,directory):
    row=worker.assert_owned(worker.inspect('container',worker.ingress))
    # Keep the legacy Compose bind source current for a future ingress recreate.
    for name,destination in [('cert.pem','/run/secrets/social-cert.pem'),('key.pem','/run/secrets/social-key.pem')]:
        path=Path(next(m for m in row['Mounts'] if m['Destination']==destination)['Source'])
        write_bound(path,(directory/name).read_bytes())


def targets(worker):
    result=[]
    for path in sorted(worker.state_dir.glob('*.json')):
        record=worker.record(path.stem); ident=record['id']
        directory=worker.root/'certificates'/('runtime-'+ident)
        if not (directory/'cert.pem').is_file() or not (directory/'key.pem').is_file():continue
        result.append({'name':ident,'kind':'runtime','instance_id':ident,'container':PROJECT+'-u-'+ident,
                       'hostname':'u-'+ident+'.'+worker.domain,'directory':str(worker.root/'certificates'/('runtime-'+ident))})
    canary=worker.assert_owned(worker.inspect('container',PROJECT+'-runtime-canary-1'))
    for name,kind,row,hostname,destination in [
        ('canary','canary',canary,'u-'+'0'*31+'1.'+worker.domain,'/run/secrets/runtime-cert.pem'),
        ('social','social',worker.assert_owned(worker.inspect('container',worker.ingress)),'social.'+worker.domain,'/run/secrets/social-cert.pem')]:
        cert=next(m for m in row['Mounts'] if m['Destination']==destination)
        directory=Path(cert['Source']).parent
        if kind=='social':
            config=json.loads((worker.route_dir/'services.yml').read_text())
            selected=config['tls']['certificates'][0]['certFile'];prefix='/etc/traefik/dynamic/'
            if selected.startswith(prefix):
                directory=worker.route_dir/Path(selected).relative_to(prefix).parent
                if not directory.resolve().is_relative_to(worker.route_dir.resolve()):raise ManagerError('social_certificate_path_conflict')
        result.append({'name':name,'kind':kind,'container':row['Name'].lstrip('/'),'hostname':hostname,'directory':str(directory)})
    return result


def recover(worker,journal):
    state=json.loads(journal.read_text());target=state['target'];backup=Path(state['previous'])
    # Journals are root-owned; constrain paths and exact identifiers anyway.
    allowed=worker.root/'certificates'
    if backup.parent.parent != worker.root/'certificate-renewals' or not (Path(target['directory']).resolve().is_relative_to(allowed) or Path(target['directory']).resolve().is_relative_to(worker.route_dir/'.tls')):
        raise ManagerError('invalid_renewal_journal')
    with paused() as locked:activate(locked,target,backup)
    if target['was_running'] or target['kind']=='social':probe(worker,target,fingerprint(backup/'cert.pem'))
    if target['kind']=='social':mirror_social(worker,backup)
    journal.unlink()
    print('renewal_recovered target='+target['name'],flush=True)


def renew(worker,target):
    current=Path(target['directory']);generation=worker.root/'certificate-renewals'/(target['name']+'-'+stamp())
    generation.mkdir(mode=0o700,parents=True)
    new=generation/'new';previous=generation/'previous';previous.mkdir(mode=0o700)
    run(['python3',str(worker.root/'release-20261020/deploy/runtime/certificates.py'),'issue','--ca',str(worker.ca),'--hostname',target['hostname'],'--output',str(new)])
    validate(new,target['hostname'],worker.ca)
    for name in ['cert.pem','key.pem']:
        atomic(previous/name,(current/name).read_text(),mode=0o600)
    row=worker.assert_owned(worker.inspect('container',target['container']),target.get('instance_id'))
    target={**target,'was_running':row['State']['Running']}
    journal=worker.root/'renewal-state'/(target['name']+'.json')
    atomic(journal,json.dumps({'target':target,'previous':str(previous),'new':str(new)},indent=2))
    try:
        if target['kind']=='social':activate(worker,target,new)
        else:
            with paused() as locked:
                if target['kind']=='runtime':locked.withdraw(target['instance_id'])
                activate(locked,target,new)
        if target['was_running'] or target['kind']=='social':probe(worker,target,fingerprint(new/'cert.pem'))
        if target['kind']=='social':mirror_social(worker,new)
        journal.unlink()
        return {'target':target['name'],'status':'renewed','fingerprint':fingerprint(new/'cert.pem'),
                'certificate':run(['openssl','x509','-in',str(new/'cert.pem'),'-noout','-enddate','-serial']).strip()}
    except Exception:
        recover(worker,journal)
        raise ManagerError('renewal_failed_previous_certificate_restored')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--force-target',help='operator drill: exact recorded instance hex ID, social or canary');args=parser.parse_args()
    if os.geteuid()!=0:raise ManagerError('root_required')
    os.umask(0o077);os.nice(10)
    with open('/run/flybot-platform-botawaken-operations.lock','w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        worker=Worker(CONFIG);state=worker.root/'renewal-state';state.mkdir(mode=0o700,exist_ok=True)
        results=[];failed=False
        # Recover an interrupted activation before any new issuance.
        for journal in sorted(state.glob('*.json')):recover(worker,journal)
        if subprocess.run(['openssl','x509','-checkend',str(365*86400),'-noout','-in',str(worker.ca/'cert.pem')],capture_output=True).returncode:
            raise ManagerError('platform_ca_renewal_required')
        available=targets(worker)
        if args.force_target and args.force_target not in {t['name'] for t in available}:raise ManagerError('unknown_renewal_target')
        for target in available:
            if args.force_target and args.force_target!=target['name']:continue
            cert=Path(target['directory'])/'cert.pem'
            due=subprocess.run(['openssl','x509','-checkend',str(RENEW_BEFORE),'-noout','-in',str(cert)],capture_output=True).returncode!=0
            if not due and not args.force_target:
                results.append({'target':target['name'],'status':'not_due',
                                'certificate':run(['openssl','x509','-in',str(cert),'-noout','-enddate','-serial']).strip()});continue
            try:
                result=renew(worker,target);results.append(result);print('renewal_complete target='+target['name'],flush=True)
            except Exception:
                failed=True;results.append({'target':target['name'],'status':'failed'});print('renewal_failed target='+target['name'],flush=True)
        report={'checked_at':stamp(),'renew_before_days':30,'results':results}
        atomic(worker.root/'certificate-renewal-status.json',json.dumps(report,indent=2))
        if failed:raise ManagerError('certificate_renewal_attention_required')
        print('certificate_check_complete targets='+str(len(results)),flush=True)


if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(str(exc) if isinstance(exc,ManagerError) else 'certificate_renewal_failed',flush=True)
        raise SystemExit(1)
