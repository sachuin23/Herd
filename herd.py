#!/usr/bin/env python
import tempfile
import sys
import os
import time
import eventlet
import re
import argparse
import logging
import configparser
from eventlet.green import socket
from eventlet.green import subprocess


parser = argparse.ArgumentParser()
parser.add_argument('local-file',
                    help='Local file to upload')

parser.add_argument('remote-file',
                    help="Remote file destination")

parser.add_argument('hosts',
                    help="File containing list of hosts",
                    default='',
                    nargs='?')

parser.add_argument('--retry',
                    default=0,
                    type=int,
                    help="Number of times to retry in case of failure. " + 
                    "Use -1 to make it retry forever (not recommended)")

parser.add_argument('--port',
                    default=8998,
                    help="Port number to run the tracker on")

parser.add_argument('--remote-path',
                    default='/tmp/herd',
                    help="Temporary path to store uploads")

parser.add_argument('--data-file',
                    default='./data',
                    help="Temporary file to store for bittornado.")

parser.add_argument('--log-dir',
                    default='/tmp/herd',
                    help="Path to the directory for murder logs")

parser.add_argument('--hostlist',
                    default=False,
                    help="Comma separated list of hots")

opts = vars(parser.parse_args())


murder_client = eventlet.import_patched('murder_client')
bttrack = eventlet.import_patched('BitTornado.BT1.track')
makemetafile = eventlet.import_patched('BitTornado.BT1.makemetafile')

username=''
password=''
log = logging.getLogger('herd')
log.setLevel(logging.DEBUG)
# create console handler with a higher log level
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)
# create formatter and add it to the handlers
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', '%Y-%m-%d %H:%M:%S')
ch.setFormatter(formatter)
# add the handlers to the log
log.addHandler(ch)

herd_root = os.path.dirname(os.path.realpath(__file__))
bittornado_tgz = os.path.join(herd_root, 'bittornado.tar.gz')
murderclient_py = os.path.join(herd_root, 'murder_client.py')

def run(local_file, remote_file, hosts):
    start = time.time()
    log.info("Spawning tracker...")
    eventlet.spawn(track)
    eventlet.sleep(1)
    local_host = (local_ip(), opts['port'])
    log.info("Creating torrent (host %s:%s)..." % local_host)
    torrent_file = mktorrent(local_file, '%s:%s' % local_host)
    log.info("Seeding %s" % torrent_file)
    eventlet.spawn(seed, torrent_file, local_file)
    log.info("Transferring")
    if not os.path.isfile(bittornado_tgz):
        cwd = os.getcwd()
        os.chdir(herd_root)
        args = ['tar', 'czf', 'bittornado.tar.gz', 'BitTornado']
        log.info("Executing: " + " ".join(args))
        subprocess.call(args)
        os.chdir(cwd)
    pool = eventlet.GreenPool(100)
    threads = []
    remainingHosts = hosts      
    for host in hosts:
        global username
        host= '%s@%s'%(username,host)
        threads.append(pool.spawn(transfer, host, torrent_file, remote_file, opts['retry']))
    for thread in threads:
        host = thread.wait()
        remainingHosts.remove(host)
        log.info("Done: %-6s Remaining: %s" % (host, remainingHosts))
    os.unlink(torrent_file)
    try:
        os.unlink(opts['data_file'])
    except OSError:
        pass
    log.info("Finished, took %.2f seconds." % (time.time() - start))


def transfer(host, local_file, remote_target, retry=0):
    rp = opts['remote_path']
    file_name = os.path.basename(local_file)
    remote_file = '%s/%s' % (rp, file_name)
    if ssh(host, 'test -d %s/BitTornado' % rp) != 0:
        ssh(host, "mkdir %s" % rp)
        scp(host, bittornado_tgz, '%s/bittornado.tar.gz' % rp)
        ssh(host, "cd %s; tar zxvf bittornado.tar.gz > /dev/null" % rp)
        scp(host, murderclient_py, '%s/murder_client.py' % rp)
    log.info("Copying %s to %s:%s" % (local_file, host, remote_file))
    scp(host, local_file, remote_file)
    command = 'python %s/murder_client.py peer %s %s' % (rp, remote_file, remote_target)
    log.info("running \"%s\" on %s" %  (command, host))
    result = ssh(host, command)
    ssh(host, 'rm %s' % remote_file)
    if result != 0:
        log.info("%s FAILED with code %s" % (host, result))
        while retry != 0:
            retry = retry - 1
            log.info("retrying on %s" % host)
            transfer(host, local_file, remote_target, 0)
    return host


#def ssh(host, command):
#    if not os.path.exists(opts['log_dir']):
#        os.makedirs(opts['log_dir'])
#        
#    with open("%s%s%s-ssh.log" % (opts['log_dir'], os.path.sep, host), 'a') as log:
#        result = subprocess.call(['ssh', '-o UserKnownHostsFile=/dev/null',
#                '-o LogLevel=quiet',
#                '-o StrictHostKeyChecking=no',
#                host, command], stdout=log,
#                stderr=log)
#    return result

def ssh(host,command):
    global password
    if not os.path.exists(opts['log_dir']):
        os.makedirs(opts['log_dir'])

    incommand =  ['sshpass','-p',password,'ssh',host,command]
    result = subprocess.call(incommand)
    print ('SSH complete')
    return result

def scp(host, local_file, remote_file):
    incommand=['sshpass','-p',password,'scp',local_file,'%s:%s' %(host,remote_file)]
    print(incommand)
    return subprocess.call(incommand,stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def mktorrent(file_name, tracker):
    torrent_file = tempfile.mkstemp('.torrent')
    makemetafile.make_meta_file(file_name, "http://%s/announce" % tracker,
                    {'target': torrent_file[1], 'piece_size_pow2': 0})
    return torrent_file[1]


def track():
    bttrack.track(["--dfile", opts['data_file'], "--port", opts['port']])


def seed(torrent, local_file):
    murder_client.run(["--responsefile", torrent,
                        "--saveas", local_file])


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("10.1.0.0", 0))
    return s.getsockname()[0]

def parseconfig():  
    config= configparser.ConfigParser()
    config.read('info.ini')
    global username;
    username=config.get('USER','user').strip('"')
    global password
    password=config.get('USER','password').strip('"')

def  herdmain():
    if not os.path.exists(opts['hosts']) and opts['hostlist'] is False:
        sys.exit('ERROR: hosts file "%s" does not exist' % opts['hosts'])

    if opts['hosts']:
        hosts = [line.strip() for line in open(opts['hosts'], 'r')]
        # filter out comments and empty lines
        hosts = [host for host in hosts if not re.match("^#", host) and not host == '']
    else:
        hosts = opts['hostlist'].split(',')
    # handles duplicates
    hosts = list(set(hosts))
    parseconfig()
    log.info("Running with options: %s" % opts)
    log.info("Running for hosts: %s" % hosts)
    run(opts['local-file'], opts['remote-file'], hosts)

herdmain()
