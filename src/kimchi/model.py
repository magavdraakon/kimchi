#
# Project Kimchi
#
# Copyright IBM, Corp. 2013
#
# Authors:
#  Adam Litke <agl@linux.vnet.ibm.com>
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301  USA

import cherrypy
import copy
import disks
import fnmatch
import functools
import glob
import ipaddr
import json
import libvirt
import logging
import os
import platform
import psutil
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid


from cherrypy.process.plugins import BackgroundTask
from cherrypy.process.plugins import SimplePlugin
from collections import defaultdict
from xml.etree import ElementTree


try:
    from collections import OrderedDict
except ImportError:
    from ordereddict import OrderedDict


from kimchi import config
from kimchi import isoinfo
from kimchi import netinfo
from kimchi import network as knetwork
from kimchi import vnc
from kimchi import xmlutils
from kimchi.asynctask import AsyncTask
from kimchi.distroloader import DistroLoader
from kimchi.exception import InvalidOperation, InvalidParameter, MissingParameter
from kimchi.exception import NotFoundError, OperationFailed
from kimchi.featuretests import FeatureTests
from kimchi.networkxml import to_network_xml
from kimchi.objectstore import ObjectStore
from kimchi.scan import Scanner
from kimchi.screenshot import VMScreenshot
from kimchi.utils import get_enabled_plugins, is_digit, kimchi_log
from kimchi.vmtemplate import VMTemplate


ISO_POOL_NAME = u'kimchi_isos'
GUESTS_STATS_INTERVAL = 5
HOST_STATS_INTERVAL = 1
VM_STATIC_UPDATE_PARAMS = {'name': './name'}
VM_LIVE_UPDATE_PARAMS = {}
STORAGE_SOURCES = {'netfs': {'addr': '/pool/source/host/@name',
                             'path': '/pool/source/dir/@path'}}


def _uri_to_name(collection, uri):
    expr = '/%s/(.*?)/?$' % collection
    m = re.match(expr, uri)
    if not m:
        raise InvalidParameter(uri)
    return m.group(1)

def template_name_from_uri(uri):
    return _uri_to_name('templates', uri)

def pool_name_from_uri(uri):
    return _uri_to_name('storagepools', uri)

def get_vm_name(vm_name, t_name, name_list):
    if vm_name:
        return vm_name
    for i in xrange(1, 1000):
        vm_name = "%s-vm-%i" % (t_name, i)
        if vm_name not in name_list:
            return vm_name
    raise OperationFailed("Unable to choose a VM name")

class Model(object):
    dom_state_map = {0: 'nostate',
                     1: 'running',
                     2: 'blocked',
                     3: 'paused',
                     4: 'shutdown',
                     5: 'shutoff',
                     6: 'crashed'}

    pool_state_map = {0: 'inactive',
                      1: 'initializing',
                      2: 'active',
                      3: 'degraded',
                      4: 'inaccessible'}

    volume_type_map = {0: 'file',
                       1: 'block',
                       2: 'directory',
                       3: 'network'}

    def __init__(self, libvirt_uri=None, objstore_loc=None):
        self.libvirt_uri = libvirt_uri or 'qemu:///system'
        self.conn = LibvirtConnection(self.libvirt_uri)
        self.objstore = ObjectStore(objstore_loc)
        self.graphics_ports = {}
        self.next_taskid = 1
        self.stats = {}
        self.host_stats = defaultdict(int)
        self.host_info = {}
        self.qemu_stream = False
        self.qemu_stream_dns = False
        self.libvirt_stream_protocols = []
        # Subscribe function to set host capabilities to be run when cherrypy
        # server is up
        # It is needed because some features tests depends on the server
        cherrypy.engine.subscribe('start', self._set_capabilities)
        self.scanner = Scanner(self._clean_scan)
        self.scanner.delete()
        self.guests_stats_thread = BackgroundTask(GUESTS_STATS_INTERVAL,
                                                  self._update_guests_stats)
        self.host_stats_thread = BackgroundTask(HOST_STATS_INTERVAL,
                                                self._update_host_stats)
        self.guests_stats_thread.start()
        self.host_stats_thread.start()

        # Please add new possible debug report command here
        # and implement the report generating function
        # based on the new report command
        self.report_tools = ({'cmd': 'sosreport --help', 'fn': self._sosreport_generate},
                             {'cmd': 'supportconfig -h', 'fn':None},
                             {'cmd': 'linuxexplorers --help', 'fn':None})

        self.distros = self._get_distros()
        if 'qemu:///' in self.libvirt_uri:
            self.host_info = self._get_host_info()
            self._default_pool_check()
            self._default_network_check()

    def _default_network_check(self):
        conn = self.conn.get()
        xml = """
             <network>
              <name>default</name>
              <forward mode='nat'/>
              <bridge name='virbr0' stp='on' delay='0' />
              <ip address='192.168.122.1' netmask='255.255.255.0'>
                <dhcp>
                  <range start='192.168.122.2' end='192.168.122.254' />
                </dhcp>
              </ip>
            </network>
        """
        try:
            net = conn.networkLookupByName("default")
        except libvirt.libvirtError:
            try:
                net = conn.networkDefineXML(xml)
            except libvirt.libvirtError, e:
                cherrypy.log.error(
                    "Fatal: Cannot create default network because of %s, exit kimchid" % e.message,
                    severity=logging.ERROR)
                sys.exit(1)

        if net.isActive() == 0:
            try:
                net.create()
            except libvirt.libvirtError, e:
                cherrypy.log.error(
                    "Fatal: Cannot activate default network because of %s, exit kimchid" % e.message,
                    severity=logging.ERROR)
                sys.exit(1)

    def _default_pool_check(self):
        default_pool = {'name': 'default',
                        'path': '/var/lib/libvirt/images',
                        'type': 'dir'}
        try:
            self.storagepools_create(default_pool)
        except InvalidOperation:
            # ignore error when pool existed
            pass
        except OperationFailed as e:
            # path used by other pool or other reasons of failure, exit
            cherrypy.log.error(
                "Fatal: Cannot create default pool because of %s, exit kimchid" % e.message,
                severity=logging.ERROR)
            sys.exit(1)

        if self.storagepool_lookup('default')['state'] == 'inactive':
            try:
                self.storagepool_activate('default')
            except OperationFailed:
                cherrypy.log.error(
                    "Fatal: Default pool cannot be activated, exit kimchid",
                    severity=logging.ERROR)
                sys.exit(1)

    def _set_capabilities(self):
        kimchi_log.info("*** Running feature tests ***")
        self.qemu_stream = FeatureTests.qemu_supports_iso_stream()
        self.qemu_stream_dns = FeatureTests.qemu_iso_stream_dns()

        self.libvirt_stream_protocols = []
        for p in ['http', 'https', 'ftp', 'ftps', 'tftp']:
            if FeatureTests.libvirt_supports_iso_stream(p):
                self.libvirt_stream_protocols.append(p)

        kimchi_log.info("*** Feature tests completed ***")
    _set_capabilities.priority = 90

    def get_capabilities(self):
        report_tool = self._get_system_report_tool()

        return {'libvirt_stream_protocols': self.libvirt_stream_protocols,
                'qemu_stream': self.qemu_stream,
                'screenshot': VMScreenshot.get_stream_test_result(),
                'system_report_tool': bool(report_tool)}

    def _update_guests_stats(self):
        vm_list = self.vms_get_list()

        for name in vm_list:
            dom = self._get_vm(name)
            vm_uuid = dom.UUIDString()
            info = dom.info()
            state = Model.dom_state_map[info[0]]

            if state != 'running':
                self.stats[vm_uuid] = {}
                continue

            if self.stats.get(vm_uuid, None) is None:
                self.stats[vm_uuid] = {}

            timestamp = time.time()
            prevStats = self.stats.get(vm_uuid, {})
            seconds = timestamp - prevStats.get('timestamp', 0)
            self.stats[vm_uuid].update({'timestamp': timestamp})

            self._get_percentage_cpu_usage(vm_uuid, info, seconds)
            self._get_network_io_rate(vm_uuid, dom, seconds)
            self._get_disk_io_rate(vm_uuid, dom, seconds)

    def _get_host_info(self):
        res = {}
        with open('/proc/cpuinfo') as f:
            for line in f.xreadlines():
                if "model name" in line:
                    res['cpu'] = line.split(':')[1].strip()
                    break

        res['memory'] = psutil.TOTAL_PHYMEM
        # 'fedora' '17' 'Beefy Miracle'
        distro, version, codename = platform.linux_distribution()
        res['os_distro'] = distro
        res['os_version'] = version
        res['os_codename'] = unicode(codename,"utf-8")

        return res

    def _get_percentage_cpu_usage(self, vm_uuid, info, seconds):
        prevCpuTime = self.stats[vm_uuid].get('cputime', 0)

        cpus = info[3]
        cpuTime = info[4] - prevCpuTime

        base = (((cpuTime) * 100.0) / (seconds * 1000.0 * 1000.0 * 1000.0))
        percentage = max(0.0, min(100.0, base / cpus))

        self.stats[vm_uuid].update({'cputime': info[4], 'cpu': percentage})

    def _get_network_io_rate(self, vm_uuid, dom, seconds):
        prevNetRxKB = self.stats[vm_uuid].get('netRxKB', 0)
        prevNetTxKB = self.stats[vm_uuid].get('netTxKB', 0)
        currentMaxNetRate = self.stats[vm_uuid].get('max_net_io', 100)

        rx_bytes = 0
        tx_bytes = 0

        tree = ElementTree.fromstring(dom.XMLDesc(0))
        for target in tree.findall('devices/interface/target'):
            dev = target.get('dev')
            io = dom.interfaceStats(dev)
            rx_bytes += io[0]
            tx_bytes += io[4]

        netRxKB = float(rx_bytes) / 1000
        netTxKB = float(tx_bytes) / 1000

        rx_stats = (netRxKB - prevNetRxKB) / seconds
        tx_stats = (netTxKB - prevNetTxKB) / seconds

        rate = rx_stats + tx_stats
        max_net_io = round(max(currentMaxNetRate, int(rate)), 1)

        self.stats[vm_uuid].update({'net_io': rate, 'max_net_io': max_net_io,
                                 'netRxKB': netRxKB, 'netTxKB': netTxKB})

    def _get_disk_io_rate(self, vm_uuid, dom, seconds):
        prevDiskRdKB = self.stats[vm_uuid].get('diskRdKB', 0)
        prevDiskWrKB = self.stats[vm_uuid].get('diskWrKB', 0)
        currentMaxDiskRate = self.stats[vm_uuid].get('max_disk_io', 100)

        rd_bytes = 0
        wr_bytes = 0

        tree = ElementTree.fromstring(dom.XMLDesc(0))
        for target in tree.findall("devices/disk/target"):
            dev = target.get("dev")
            io = dom.blockStats(dev)
            rd_bytes += io[1]
            wr_bytes += io[3]

        diskRdKB = float(rd_bytes) / 1024
        diskWrKB = float(wr_bytes) / 1024

        rd_stats = (diskRdKB - prevDiskRdKB) / seconds
        wr_stats = (diskWrKB - prevDiskWrKB) / seconds

        rate = rd_stats + wr_stats
        max_disk_io = round(max(currentMaxDiskRate, int(rate)), 1)

        self.stats[vm_uuid].update({'disk_io': rate, 'max_disk_io': max_disk_io,
                                 'diskRdKB': diskRdKB, 'diskWrKB': diskWrKB})

    def debugreport_lookup(self, name):
        path = config.get_debugreports_path()
        file_pattern = os.path.join(path, name)
        file_pattern = file_pattern + '.*'
        try:
            file_target = glob.glob(file_pattern)[0]
        except IndexError:
            raise NotFoundError('no such report')

        ctime = os.stat(file_target).st_ctime
        ctime = time.strftime("%Y-%m-%d-%H:%M:%S", time.localtime(ctime))
        file_target = os.path.split(file_target)[-1]
        file_target = os.path.join("/data/debugreports", file_target)
        return {'file': file_target,
                'ctime': ctime}

    def debugreportcontent_lookup(self, name):
        return self.debugreport_lookup(name)

    def debugreport_delete(self, name):
        path = config.get_debugreports_path()
        file_pattern = os.path.join(path, name + '.*')
        try:
            file_target = glob.glob(file_pattern)[0]
        except IndexError:
            raise NotFoundError('no such report')

        os.remove(file_target)

    def debugreports_create(self, params):
        ident = params['name']
        taskid = self._gen_debugreport_file(ident)
        return self.task_lookup(taskid)

    def debugreports_get_list(self):
        path = config.get_debugreports_path()
        file_pattern = os.path.join(path, '*.*')
        file_lists = glob.glob(file_pattern)
        file_lists = [os.path.split(file)[1] for file in file_lists]
        name_lists = [file.split('.', 1)[0] for file in file_lists]

        return name_lists

    def  _update_host_stats(self):
        preTimeStamp = self.host_stats['timestamp']
        timestamp = time.time()
        # FIXME when we upgrade psutil, we can get uptime by psutil.uptime
        # we get uptime by float(open("/proc/uptime").readline().split()[0])
        # and calculate the first io_rate after the OS started.
        seconds = (timestamp - preTimeStamp if preTimeStamp else
                   float(open("/proc/uptime").readline().split()[0]))

        self.host_stats['timestamp'] = timestamp
        self._get_host_disk_io_rate(seconds)
        self._get_host_network_io_rate(seconds)

        self._get_percentage_host_cpu_usage()
        self._get_host_memory_stats()

    def _get_percentage_host_cpu_usage(self):
        # This is cpu usage producer. This producer will calculate the usage
        # at an interval of HOST_STATS_INTERVAL.
        # The psutil.cpu_percent works as non blocking.
        # psutil.cpu_percent maintains a cpu time sample.
        # It will update the cpu time sample when it is called.
        # So only this producer can call psutil.cpu_percent in kimchi.
        self.host_stats['cpu_utilization'] = psutil.cpu_percent(None)

    def _get_host_memory_stats(self):
        virt_mem = psutil.virtual_memory()
        # available:
        #  the actual amount of available memory that can be given
        #  instantly to processes that request more memory in bytes; this
        #  is calculated by summing different memory values depending on
        #  the platform (e.g. free + buffers + cached on Linux)
        memory_stats = {'total': virt_mem.total,
                        'free': virt_mem.free,
                        'cached': virt_mem.cached,
                        'buffers': virt_mem.buffers,
                        'avail': virt_mem.available}
        self.host_stats['memory'] = memory_stats

    def _get_host_disk_io_rate(self, seconds):
        prev_read_bytes = self.host_stats['disk_read_bytes']
        prev_write_bytes = self.host_stats['disk_write_bytes']

        disk_io = psutil.disk_io_counters(False)
        read_bytes = disk_io.read_bytes
        write_bytes = disk_io.write_bytes

        rd_rate = int(float(read_bytes - prev_read_bytes) / seconds + 0.5)
        wr_rate = int(float(write_bytes - prev_write_bytes) / seconds + 0.5)

        self.host_stats.update({'disk_read_rate': rd_rate,
                                'disk_write_rate': wr_rate,
                                'disk_read_bytes': read_bytes,
                                'disk_write_bytes': write_bytes})

    def _get_host_network_io_rate(self, seconds):
        prev_recv_bytes = self.host_stats['net_recv_bytes']
        prev_sent_bytes = self.host_stats['net_sent_bytes']

        net_ios = psutil.network_io_counters(True)
        recv_bytes = 0
        sent_bytes = 0
        for key in set(netinfo.nics() +
                       netinfo.wlans()) & set(net_ios.iterkeys()):
            recv_bytes = recv_bytes + net_ios[key].bytes_recv
            sent_bytes = sent_bytes + net_ios[key].bytes_sent

        rx_rate = int(float(recv_bytes - prev_recv_bytes) / seconds + 0.5)
        tx_rate = int(float(sent_bytes - prev_sent_bytes) / seconds + 0.5)

        self.host_stats.update({'net_recv_rate': rx_rate,
                                'net_sent_rate': tx_rate,
                                'net_recv_bytes': recv_bytes,
                                'net_sent_bytes': sent_bytes})

    def _static_vm_update(self, dom, params):
        state = Model.dom_state_map[dom.info()[0]]

        old_xml = new_xml = dom.XMLDesc(0)

        for key, val in params.items():
            if key in VM_STATIC_UPDATE_PARAMS:
                new_xml = xmlutils.xml_item_update(new_xml, VM_STATIC_UPDATE_PARAMS[key], val)

        try:
            if 'name' in params:
                if state == 'running':
                    raise InvalidParameter("vm name can just updated when vm shutoff")
                else:
                    dom.undefine()
            conn = self.conn.get()
            dom = conn.defineXML(new_xml)
        except libvirt.libvirtError as e:
            dom = conn.defineXML(old_xml)
            raise OperationFailed(e.get_error_message())
        return dom

    def _live_vm_update(self, dom, params):
        pass

    def vm_update(self, name, params):
        dom = self._get_vm(name)
        dom = self._static_vm_update(dom, params)
        self._live_vm_update(dom, params)
        return dom.name()

    def vm_lookup(self, name):
        dom = self._get_vm(name)
        info = dom.info()
        state = Model.dom_state_map[info[0]]
        screenshot = None
        graphics_type, _ = self._vm_get_graphics(name)
        # 'port' must remain None until a connect call is issued
        graphics_port = (self.graphics_ports.get(name, None) if state == 'running'
                      else None)
        try:
            if state == 'running':
                screenshot = self.vmscreenshot_lookup(name)
            elif state == 'shutoff':
                # reset vm stats when it is powered off to avoid sending
                # incorrect (old) data
                self.stats[dom.UUIDString()] = {}
        except NotFoundError:
            pass

        with self.objstore as session:
            try:
                extra_info = session.get('vm', dom.UUIDString())
            except NotFoundError:
                extra_info = {}
        icon = extra_info.get('icon')

        vm_stats = self.stats.get(dom.UUIDString(), {})
        stats = {}
        stats['cpu_utilization'] = vm_stats.get('cpu', 0)
        stats['net_throughput'] = vm_stats.get('net_io', 0)
        stats['net_throughput_peak'] = vm_stats.get('max_net_io', 100)
        stats['io_throughput'] = vm_stats.get('disk_io', 0)
        stats['io_throughput_peak'] = vm_stats.get('max_disk_io', 100)

        return {'state': state,
                'stats': str(stats),
                'uuid': dom.UUIDString(),
                'memory': info[2] >> 10,
                'cpus': info[3],
                'screenshot': screenshot,
                'icon': icon,
                'graphics': {"type": graphics_type, "port": graphics_port}}

    def _vm_get_disk_paths(self, dom):
        xml = dom.XMLDesc(0)
        xpath = "/domain/devices/disk[@device='disk']/source/@file"
        return xmlutils.xpath_get_text(xml, xpath)

    def vm_delete(self, name):
        if self._vm_exists(name):
            conn = self.conn.get()
            dom = self._get_vm(name)
            self._vmscreenshot_delete(dom.UUIDString())
            paths = self._vm_get_disk_paths(dom)
            info = self.vm_lookup(name)

            if info['state'] == 'running':
                self.vm_stop(name)

            dom.undefine()

            for path in paths:
                vol = conn.storageVolLookupByPath(path)
                vol.delete(0)

            with self.objstore as session:
                session.delete('vm', dom.UUIDString(), ignore_missing=True)

    def vm_start(self, name):
        dom = self._get_vm(name)
        dom.create()

    def vm_stop(self, name):
        if self._vm_exists(name):
            dom = self._get_vm(name)
            dom.destroy()

    def _vm_get_graphics(self, name):
        dom = self._get_vm(name)
        xml = dom.XMLDesc(0)
        expr = "/domain/devices/graphics/@type"
        res = xmlutils.xpath_get_text(xml, expr)
        graphics_type = res[0] if res else None
        port = None
        if graphics_type:
            expr = "/domain/devices/graphics[@type='%s']/@port" % graphics_type
            res = xmlutils.xpath_get_text(xml, expr)
            port = int(res[0]) if res else None
        # FIX ME
        # graphics_type should be 'vnc' or None.  'spice' should only be
        # returned if we support it in the future.
        graphics_type = None if graphics_type != "vnc" else graphics_type
        return graphics_type, port

    def vm_connect(self, name):
        graphics, port = self._vm_get_graphics(name)
        if graphics == "vnc" and port != None:
            port = vnc.new_ws_proxy(port)
            self.graphics_ports[name] = port
        else:
            raise OperationFailed("Unable to find VNC port in %s" % name)

    def vms_create(self, params):
        conn = self.conn.get()
        t_name = template_name_from_uri(params['template'])
        vm_uuid = str(uuid.uuid4())
        vm_list = self.vms_get_list()
        name = get_vm_name(params.get('name'), t_name, vm_list)
        # incoming text, from js json, is unicode, do not need decode
        if name in vm_list:
            raise InvalidOperation("VM already exists")

        vm_overrides = dict()
        pool_uri = params.get('storagepool')
        if pool_uri:
            vm_overrides['storagepool'] = pool_uri
        t = self._get_template(t_name, vm_overrides)

        if not self.qemu_stream and t.info.get('iso_stream', False):
            raise InvalidOperation("Remote ISO image is not supported by this server.")

        t.validate()
        vol_list = t.fork_vm_storage(vm_uuid)

        # Store the icon for displaying later
        icon = t.info.get('icon')
        if icon:
            with self.objstore as session:
                session.store('vm', vm_uuid, {'icon': icon})

        libvirt_stream = False if len(self.libvirt_stream_protocols) == 0 else True

        xml = t.to_vm_xml(name, vm_uuid, libvirt_stream, self.qemu_stream_dns)
        try:
            dom = conn.defineXML(xml.encode('utf-8'))
        except libvirt.libvirtError as e:
            for v in vol_list:
                vol = conn.storageVolLookupByPath(v['path'])
                vol.delete(0)
            raise OperationFailed(e.get_error_message())
        return name

    def vms_get_list(self):
        conn = self.conn.get()
        ids = conn.listDomainsID()
        names = map(lambda x: conn.lookupByID(x).name(), ids)
        names += conn.listDefinedDomains()
        names = map(lambda x: x.decode('utf-8'), names)
        return sorted(names, key=unicode.lower)

    def vmscreenshot_lookup(self, name):
        dom = self._get_vm(name)
        d_info = dom.info()
        vm_uuid = dom.UUIDString()
        if Model.dom_state_map[d_info[0]] != 'running':
            raise NotFoundError('No screenshot for stopped vm')

        screenshot = self._get_screenshot(vm_uuid)
        img_path = screenshot.lookup()
        # screenshot info changed after scratch generation
        with self.objstore as session:
            session.store('screenshot', vm_uuid, screenshot.info)
        return img_path

    def _vmscreenshot_delete(self, vm_uuid):
        screenshot = self._get_screenshot(vm_uuid)
        screenshot.delete()
        with self.objstore as session:
            session.delete('screenshot', vm_uuid)

    def template_lookup(self, name):
        t = self._get_template(name)
        return t.info

    def template_delete(self, name):
        with self.objstore as session:
            session.delete('template', name)

    def templates_create(self, params):
        name = params['name']
        with self.objstore as session:
            if name in session.get_list('template'):
                raise InvalidOperation("Template already exists")
            t = LibvirtVMTemplate(params, scan=True)
            session.store('template', name, t.info)
        return name

    def template_update(self, name, params):
        old_t = self.template_lookup(name)
        new_t = copy.copy(old_t)

        new_t.update(params)
        ident = name

        new_storagepool = new_t.get(u'storagepool', '')
        try:
            self._get_storagepool(pool_name_from_uri(new_storagepool))
        except Exception as e:
            raise InvalidParameter("Storagepool specified is not valid: %s." % e.message)

        self.template_delete(name)
        try:
            ident = self.templates_create(new_t)
        except:
            ident = self.templates_create(old_t)
            raise
        return ident

    def templates_get_list(self):
        with self.objstore as session:
            return session.get_list('template')

    def interfaces_get_list(self):
        return list(set(netinfo.all_favored_interfaces()) -
                    set(self._get_all_networks_interfaces()))

    def interface_lookup(self, name):
        try:
            return netinfo.get_interface_info(name)
        except ValueError, e:
            raise NotFoundError(e)

    def _get_network(self, name):
        conn = self.conn.get()
        try:
            return conn.networkLookupByName(name)
        except libvirt.libvirtError as e:
            raise NotFoundError("Network '%s' not found: %s" %
                                (name, e.get_error_message()))

    def _get_network_from_xml(self, xml):
        address = xmlutils.xpath_get_text(xml, "/network/ip/@address")
        address = address and address[0] or ''
        netmask = xmlutils.xpath_get_text(xml, "/network/ip/@netmask")
        netmask = netmask and netmask[0] or ''
        net = address and netmask and "/".join([address, netmask]) or ''

        dhcp_start = xmlutils.xpath_get_text(xml,
                                             "/network/ip/dhcp/range/@start")
        dhcp_start = dhcp_start and dhcp_start[0] or ''
        dhcp_end = xmlutils.xpath_get_text(xml, "/network/ip/dhcp/range/@end")
        dhcp_end = dhcp_end and dhcp_end[0] or ''
        dhcp = {'start': dhcp_start, 'end': dhcp_end}

        forward_mode = xmlutils.xpath_get_text(xml, "/network/forward/@mode")
        forward_mode = forward_mode and forward_mode[0] or ''
        forward_if = xmlutils.xpath_get_text(xml,
                                             "/network/forward/interface/@dev")
        forward_pf = xmlutils.xpath_get_text(xml, "/network/forward/pf/@dev")
        bridge = xmlutils.xpath_get_text(xml, "/network/bridge/@name")
        bridge = bridge and bridge[0] or ''
        return {'subnet': net, 'dhcp': dhcp, 'bridge': bridge,
                'forward': {'mode': forward_mode,
                            'interface': forward_if,
                            'pf': forward_pf}}

    def _get_all_networks_interfaces(self):
        net_names = self.networks_get_list()
        interfaces = []
        for name in net_names:
            network = self._get_network(name)
            xml = network.XMLDesc(0)
            net_dict = self._get_network_from_xml(xml)
            forward = net_dict['forward']
            (forward['mode'] == 'bridge' and forward['interface'] and
             interfaces.append(forward['interface'][0]) is None or
             interfaces.extend(forward['interface'] + forward['pf']))
            net_dict['bridge'] and interfaces.append(net_dict['bridge'])
        return interfaces

    def _set_network_subnet(self, params):
        netaddr = params.get('subnet', '')
        net_addrs = []
        # lookup a free network address for nat and isolated automatically
        if not netaddr:
            for net_name in self.networks_get_list():
                network = self._get_network(net_name)
                xml = network.XMLDesc(0)
                subnet = self._get_network_from_xml(xml)['subnet']
                subnet and net_addrs.append(ipaddr.IPNetwork(subnet))
            netaddr = knetwork.get_one_free_network(net_addrs)
            if not netaddr:
                raise OperationFailed("can not find a free IP address "
                                      "for network '%s'" %
                                      params['name'])
        try:
            ip = ipaddr.IPNetwork(netaddr)
        except ValueError as e:
            raise InvalidParameter("%s" % e)
        if ip.ip == ip.network:
            ip.ip = ip.ip + 1
        dhcp_start = str(ip.ip + ip.numhosts / 2)
        dhcp_end = str(ip.ip + ip.numhosts - 2)
        params.update({'net': str(ip),
                       'dhcp': {'range': {'start': dhcp_start,
                                'end': dhcp_end}}})

    def _set_network_bridge(self, params):
        try:
            iface = params['interface']
            if iface in self._get_all_networks_interfaces():
                raise InvalidParameter("interface '%s' already in use." %
                                       iface)
        except KeyError, e:
            raise MissingParameter(e)
        if netinfo.is_bridge(iface):
            params['bridge'] = iface
        elif netinfo.is_bare_nic(iface) or netinfo.is_bonding(iface):
            params['forward']['dev'] = iface
        else:
            raise InvalidParameter("the interface should be bare nic, "
                                   "bonding or bridge device.")

    def networks_create(self, params):
        conn = self.conn.get()
        name = params['name']
        if name in self.networks_get_list():
            raise InvalidOperation("Network %s already exists" % name)

        connection = params["connection"]
        # set forward mode, isolated do not need forward
        if connection != 'isolated':
            params['forward'] = {'mode': connection}

        # set subnet, bridge network do not need subnet
        if connection in ["nat", 'isolated']:
            self._set_network_subnet(params)

        # only bridge network need bridge(linux bridge) or interface(macvtap)
        if connection == 'bridge':
            self._set_network_bridge(params)

        xml = to_network_xml(**params)

        try:
            network = conn.networkDefineXML(xml)
            network.setAutostart(True)
        except libvirt.libvirtError as e:
            raise OperationFailed(e.get_error_message())

        return name

    def networks_get_list(self):
        conn = self.conn.get()
        return sorted(conn.listNetworks() + conn.listDefinedNetworks())

    def network_lookup(self, name):
        network = self._get_network(name)
        xml = network.XMLDesc(0)
        net_dict = self._get_network_from_xml(xml)
        subnet = net_dict['subnet']
        dhcp = net_dict['dhcp']
        forward = net_dict['forward']
        interface = net_dict['bridge']

        connection = forward['mode'] or "isolated"
        # FIXME, if we want to support other forward mode well.
        if connection == 'bridge':
            # macvtap bridge
            interface = interface or forward['interface'][0]
            # exposing the network on linux bridge or macvtap interface
            interface_subnet = knetwork.get_dev_netaddr(interface)
            subnet = subnet if subnet else interface_subnet

        # libvirt use format 192.168.0.1/24, standard should be 192.168.0.0/24
        # http://www.ovirt.org/File:Issue3.png
        if subnet:
            subnet = ipaddr.IPNetwork(subnet)
            subnet = "%s/%s" % (subnet.network, subnet.prefixlen)

        return {'connection': connection,
                'interface': interface,
                'subnet': subnet,
                'dhcp': dhcp,
                'autostart': network.autostart() == 1,
                'state':  network.isActive() and "active" or "inactive"}

    def network_activate(self, name):
        network = self._get_network(name)
        network.create()

    def network_deactivate(self, name):
        network = self._get_network(name)
        network.destroy()

    def network_delete(self, name):
        network = self._get_network(name)
        if network.isActive():
            raise InvalidOperation(
                "Unable to delete the active network %s" % name)
        network.undefine()

    def add_task(self, target_uri, fn, opaque=None):
        id = self.next_taskid
        self.next_taskid = self.next_taskid + 1

        task = AsyncTask(id, target_uri, fn, self.objstore, opaque)

        return id

    def tasks_get_list(self):
        with self.objstore as session:
            return session.get_list('task')

    def task_lookup(self, id):
        with self.objstore as session:
            return session.get('task', str(id))

    def _vm_exists(self, name):
        try:
            self._get_vm(name)
            return True
        except NotFoundError:
            return False
        except:
            raise


    def _get_vm(self, name):
        conn = self.conn.get()
        try:
            # outgoing text to libvirt, encode('utf-8')
            return conn.lookupByName(name.encode("utf-8"))
        except libvirt.libvirtError as e:
            if e.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                raise NotFoundError("Virtual Machine '%s' not found" % name)
            else:
                raise

    def _get_template(self, name, overrides=None):
        with self.objstore as session:
            params = session.get('template', name)
        if overrides:
            params.update(overrides)
        return LibvirtVMTemplate(params, False, self.conn)

    def isopool_lookup(self, name):
        return {'state': 'active',
                'type': 'kimchi-iso'}

    def isovolumes_get_list(self):
        iso_volumes = []
        pools = self.storagepools_get_list()

        for pool in pools:
            try:
                volumes = self.storagevolumes_get_list(pool)
            except InvalidOperation:
                # Skip inactive pools
                continue
            for volume in volumes:
                res = self.storagevolume_lookup(pool, volume)
                if res['format'] == 'iso':
                    res['name'] = '%s' % volume
                    iso_volumes.append(res)
        return iso_volumes

    def _clean_scan(self, pool_name):
        try:
            self.storagepool_deactivate(pool_name)
            with self.objstore as session:
                session.delete('scanning', pool_name)
        except Exception, e:
            kimchi_log.debug("Exception %s occured when cleaning scan result" % e.message)

    def _do_deep_scan(self, params):
        scan_params = dict(ignore_list=[])
        scan_params['scan_path'] = params['path']
        params['type'] = 'dir'

        for pool in self.storagepools_get_list():
            try:
                res = self.storagepool_lookup(pool)
                if res['state'] == 'active':
                    scan_params['ignore_list'].append(res['path'])
            except Exception, e:
                kimchi_log.debug("Exception %s occured when get ignore path" % e.message)

        params['path'] = scan_params['pool_path'] = self.scanner.scan_dir_prepare(
            params['name'])
        task_id = self.add_task('', self.scanner.start_scan, scan_params)
        # Record scanning-task/storagepool mapping for future querying
        with self.objstore as session:
                session.store('scanning', params['name'], task_id)
        return task_id

    def storagepools_create(self, params):
        task_id = None
        conn = self.conn.get()
        try:
            name = params['name']
            if name in (ISO_POOL_NAME, ):
                raise InvalidOperation("StoragePool already exists")

            if params['type'] == 'kimchi-iso':
                task_id = self._do_deep_scan(params)
            xml = _get_pool_xml(**params)
        except KeyError, key:
            raise MissingParameter(key)

        if name in self.storagepools_get_list():
            raise InvalidOperation(
                        "The name %s has been used by a pool" % name)

        try:
            if task_id:
                # Create transient pool for deep scan
                conn.storagePoolCreateXML(xml, 0)
                return name

            pool = conn.storagePoolDefineXML(xml, 0)
            if params['type'] in ['logical', 'dir']:
                pool.build(libvirt.VIR_STORAGE_POOL_BUILD_NEW)
                # autostart dir and logical storage pool created from kimchi
                pool.setAutostart(1)
            else:
                # disable autostart for others
                pool.setAutostart(0)
        except libvirt.libvirtError as e:
            msg = "Problem creating Storage Pool: %s"
            kimchi_log.error(msg, e)
            raise OperationFailed(e.get_error_message())
        return name

    def _get_storage_source(self, pool_type, pool_xml):
        source = {}
        if pool_type not in STORAGE_SOURCES:
            return source

        for key, val in STORAGE_SOURCES[pool_type].items():
            res = xmlutils.xpath_get_text(pool_xml, val)
            source[key] = res[0] if len(res) == 1 else res

        return source

    def storagepool_lookup(self, name):
        pool = self._get_storagepool(name)
        info = pool.info()
        nr_volumes = self._get_storagepool_vols_num(pool)
        autostart = True if pool.autostart() else False
        xml = pool.XMLDesc(0)
        path = xmlutils.xpath_get_text(xml, "/pool/target/path")[0]
        pool_type = xmlutils.xpath_get_text(xml, "/pool/@type")[0]
        source = self._get_storage_source(pool_type, xml)
        res = {'state': Model.pool_state_map[info[0]],
               'path': path,
               'source': source,
               'type': pool_type,
               'autostart': autostart,
               'capacity': info[1],
               'allocated': info[2],
               'available': info[3],
               'nr_volumes': nr_volumes}

        if not pool.isPersistent():
            # Deal with deep scan generated pool
            try:
                with self.objstore as session:
                    task_id = session.get('scanning', name)
                res['task_id'] = str(task_id)
                res['type'] = 'kimchi-iso'
            except NotFoundError:
                # User created normal pool
                pass
        return res

    def storagepool_update(self, name, params):
        autostart = params['autostart']
        if autostart not in [True, False]:
            raise InvalidOperation("Autostart flag must be true or false")
        pool = self._get_storagepool(name)
        if autostart:
            pool.setAutostart(1)
        else:
            pool.setAutostart(0)
        ident = pool.name()
        return ident

    def storagepool_activate(self, name):
        pool = self._get_storagepool(name)
        try:
            pool.create(0)
        except libvirt.libvirtError as e:
            raise OperationFailed(e.get_error_message())

    def storagepool_deactivate(self, name):
        pool = self._get_storagepool(name)
        try:
            pool.destroy()
        except libvirt.libvirtError as e:
            raise OperationFailed(e.get_error_message())

    def _pool_refresh(self, pool):
        try:
            pool.refresh(0)
        except libvirt.libvirtError as e:
            raise OperationFailed(e.get_error_message())

    def _get_storagepool_vols_num(self, pool):
        try:
            if pool.isActive():
                self._pool_refresh(pool)
                return pool.numOfVolumes()
            else:
                return 0
        except libvirt.libvirtError as e:
            raise OperationFailed(e.get_error_message())

    def storagepool_delete(self, name):
        pool = self._get_storagepool(name)
        if pool.isActive():
            raise InvalidOperation(
                        "Unable to delete the active storagepool %s" % name)
        try:
            pool.undefine()
        except libvirt.libvirtError as e:
            raise OperationFailed(e.get_error_message())

    def storagepools_get_list(self):
        try:
            conn = self.conn.get()
            names = conn.listStoragePools()
            names += conn.listDefinedStoragePools()
            return sorted(names)
        except libvirt.libvirtError as e:
            raise OperationFailed(e.get_error_message())

    def _get_storagepool(self, name):
        conn = self.conn.get()
        try:
            return conn.storagePoolLookupByName(name)
        except libvirt.libvirtError as e:
            if e.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_POOL:
                raise NotFoundError("Storage Pool '%s' not found" % name)
            else:
                raise

    def storagevolumes_create(self, pool, params):
        info = self.storagepool_lookup(pool)
        try:
            name = params['name']
            xml = _get_volume_xml(**params)
        except KeyError, key:
            raise MissingParameter(key)
        pool = self._get_storagepool(pool)
        try:
            pool.createXML(xml, 0)
        except libvirt.libvirtError as e:
            raise OperationFailed(e.get_error_message())
        return name

    def storagevolume_lookup(self, pool, name):
        vol = self._get_storagevolume(pool, name)
        path = vol.path()
        info = vol.info()
        xml = vol.XMLDesc(0)
        fmt = xmlutils.xpath_get_text(xml, "/volume/target/format/@type")[0]
        res = dict(type=Model.volume_type_map[info[0]],
                   capacity=info[1],
                   allocation=info[2],
                   path=path,
                   format=fmt)
        if fmt == 'iso':
            if os.path.islink(path):
                path = os.path.join(os.path.dirname(path), os.readlink(path))
            os_distro = os_version = 'unknown'
            try:
                os_distro, os_version = isoinfo.probe_one(path)
                bootable = True
            except isoinfo.IsoFormatError:
                bootable = False
            res.update(
                dict(os_distro=os_distro, os_version=os_version, path=path, bootable=bootable))

        return res

    def storagevolume_wipe(self, pool, name):
        volume = self._get_storagevolume(pool, name)
        try:
            volume.wipePattern(libvirt.VIR_STORAGE_VOL_WIPE_ALG_ZERO, 0)
        except libvirt.libvirtError as e:
            raise OperationFailed(e.get_error_message())

    def storagevolume_delete(self, pool, name):
        volume = self._get_storagevolume(pool, name)
        try:
            volume.delete(0)
        except libvirt.libvirtError as e:
            raise OperationFailed(e.get_error_message())

    def storagevolume_resize(self, pool, name, size):
        size = size << 20
        volume = self._get_storagevolume(pool, name)
        try:
            volume.resize(size, 0)
        except libvirt.libvirtError as e:
            raise OperationFailed(e.get_error_message())

    def storagevolumes_get_list(self, pool):
        pool = self._get_storagepool(pool)
        if not pool.isActive():
            raise InvalidOperation(
            "Unable to list volumes in inactive storagepool %s" % pool.name())
        try:
            self._pool_refresh(pool)
            return pool.listVolumes()
        except libvirt.libvirtError as e:
            raise OperationFailed(e.get_error_message())

    def _get_storagevolume(self, pool, name):
        pool = self._get_storagepool(pool)
        if not pool.isActive():
            raise InvalidOperation(
            "Unable to list volumes in inactive storagepool %s" % pool.name())
        try:
            return pool.storageVolLookupByName(name)
        except libvirt.libvirtError as e:
            if e.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_VOL:
                raise NotFoundError("Storage Volume '%s' not found" % name)
            else:
                raise

    def _get_screenshot(self, vm_uuid):
        with self.objstore as session:
            try:
                params = session.get('screenshot', vm_uuid)
            except NotFoundError:
                params = {'uuid': vm_uuid}
                session.store('screenshot', vm_uuid, params)
        return LibvirtVMScreenshot(params, self.conn)

    def _sosreport_generate(self, cb, name):
        command = 'sosreport --batch --name "%s"' % name
        try:
            retcode = subprocess.call(command, shell=True, stdout=subprocess.PIPE)
            if retcode < 0:
                raise OperationFailed('Command terminated with signal')
            elif retcode > 0:
                raise OperationFailed('Command failed: rc = %i' % retcode)
            pattern = '/tmp/sosreport-%s-*' % name
            for reportFile in glob.glob(pattern):
                if not fnmatch.fnmatch(reportFile, '*.md5'):
                    output = reportFile
                    break
            else:
                # sosreport tends to change the name mangling rule and
                # compression file format between different releases.
                # It's possible to fail to match a report file even sosreport
                # runs successfully. In future we might have a general name
                # mangling function in kimchi to format the name before passing
                # it to sosreport. Then we can delete this exception.
                raise OperationFailed('Can not find generated debug report '
                                      'named by %s' % pattern)
            ext = output.split('.', 1)[1]
            path = config.get_debugreports_path()
            target = os.path.join(path, name)
            target_file = '%s.%s' % (target, ext)
            shutil.move(output, target_file)
            os.remove('%s.md5' % output)
            cb('OK', True)

            return

        except OSError:
            raise

        except Exception, e:
            # No need to call cb to update the task status here.
            # The task object will catch the exception rasied here
            # and update the task status there
            log = logging.getLogger('Model')
            log.warning('Exception in generating debug file: %s', e)
            raise OperationFailed(e)

    def _get_system_report_tool(self):
        # check if the command can be found by shell one by one
        for helper_tool in self.report_tools:
            try:
                retcode = subprocess.call(helper_tool['cmd'], shell=True,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if retcode == 0:
                    return helper_tool['fn']
            except Exception, e:
                kimchi_log.info('Exception running command: %s', e)

        return None

    def _gen_debugreport_file(self, name):
        gen_cmd = self._get_system_report_tool()

        if gen_cmd is not None:
            return self.add_task('', gen_cmd, name)

        raise OperationFailed("debugreport tool not found")

    def _get_distros(self):
        distroloader = DistroLoader()
        return  distroloader.get()

    def distros_get_list(self):
        return self.distros.keys()

    def distro_lookup(self, name):
        try:
            return self.distros[name]
        except KeyError:
            raise NotFoundError("distro '%s' not found" % name)

    def host_lookup(self, *name):
        return self.host_info

    def hoststats_lookup(self, *name):
        return {'cpu_utilization': self.host_stats['cpu_utilization'],
                'memory': self.host_stats.get('memory'),
                'disk_read_rate': self.host_stats['disk_read_rate'],
                'disk_write_rate': self.host_stats['disk_write_rate'],
                'net_recv_rate': self.host_stats['net_recv_rate'],
                'net_sent_rate': self.host_stats['net_sent_rate']}

    def plugins_get_list(self):
        return [plugin for (plugin, config) in get_enabled_plugins()]

    def partitions_get_list(self):
        result = disks.get_partitions_names()
        return result

    def partition_lookup(self, name):
        if name not in disks.get_partitions_names():
            raise NotFoundError("Partition %s not found in the host"
                                % name)
        return disks.get_partition_details(name)

    def vms_get_list_by_state(self, state):
        ret_list = []
        for name in self.vms_get_list():
            info = self._get_vm(name).info()
            if (Model.dom_state_map[info[0]]) == state:
                ret_list.append(name)
        return ret_list

    def host_shutdown(self, args=None):
        # Check for running vms before shutdown
        running_vms = self.vms_get_list_by_state('running')
        if len(running_vms) > 0:
            raise OperationFailed("Shutdown not allowed: VMs are running!")
        kimchi_log.info('Host is going to shutdown.')
        os.system('shutdown -h now')

    def host_reboot(self, args=None):
        # Find running VMs
        running_vms = self.vms_get_list_by_state('running')
        if len(running_vms) > 0:
            raise OperationFailed("Reboot not allowed: VMs are running!")
        kimchi_log.info('Host is going to reboot.')
        os.system('reboot')


class LibvirtVMTemplate(VMTemplate):
    def __init__(self, args, scan=False, conn=None):
        VMTemplate.__init__(self, args, scan)
        self.conn = conn

    def _storage_validate(self):
        pool_uri = self.info['storagepool']
        pool_name = pool_name_from_uri(pool_uri)
        try:
            conn = self.conn.get()
            pool = conn.storagePoolLookupByName(pool_name)
        except libvirt.libvirtError:
            raise InvalidParameter('Storage specified by template does not exist')
        if not pool.isActive():
            raise InvalidParameter('Storage specified by template is not active')

        return pool

    def _network_validate(self):
        name = self.info['network']
        try:
            conn = self.conn.get()
            network = conn.networkLookupByName(name)
        except libvirt.libvirtError:
            raise InvalidParameter('Network specified by template does not exist')
        if not network.isActive():
            raise InvalidParameter('Storage specified by template is not active')

        return network

    def _get_storage_path(self):
        pool = self._storage_validate()
        xml = pool.XMLDesc(0)
        return xmlutils.xpath_get_text(xml, "/pool/target/path")[0]

    def fork_vm_storage(self, vm_uuid):
        # Provision storage:
        # TODO: Rebase on the storage API once upstream
        pool = self._storage_validate()
        vol_list = self.to_volume_list(vm_uuid)
        for v in vol_list:
            # outgoing text to libvirt, encode('utf-8')
            pool.createXML(v['xml'].encode('utf-8'), 0)
        return vol_list


class LibvirtVMScreenshot(VMScreenshot):
    def __init__(self, vm_uuid, conn):
        VMScreenshot.__init__(self, vm_uuid)
        self.conn = conn

    def _generate_scratch(self, thumbnail):
        def handler(stream, buf, opaque):
            fd = opaque
            os.write(fd, buf)

        fd = os.open(thumbnail, os.O_WRONLY | os.O_TRUNC | os.O_CREAT, 0644)
        try:
            conn = self.conn.get()
            dom = conn.lookupByUUIDString(self.vm_uuid)
            vm_name = dom.name()
            stream = conn.newStream(0)
            mimetype = dom.screenshot(stream, 0, 0)
            stream.recvAll(handler, fd)
        except libvirt.libvirtError:
            try:
                stream.abort()
            except:
                pass
            raise NotFoundError("Screenshot not supported for %s" % vm_name)
        else:
            stream.finish()
        finally:
            os.close(fd)

def _get_pool_xml(**kwargs):
    # Required parameters
    # name:
    # type:
    # path:
    # nfspath:
    if kwargs['type'] == 'dir':
        xml = """
        <pool type='%(type)s'>
          <name>%(name)s</name>
          <target>
            <path>%(path)s</path>
          </target>
        </pool>
       """
    elif kwargs['type'] == 'netfs':
        path = '/var/lib/kimchi/nfs_mount/' + kwargs['name'];
        if not os.path.exists(path):
           os.makedirs(path)
        kwargs.update({'path': path})
        xml = """
        <pool type='%(type)s'>
          <name>%(name)s</name>
          <source>
            <host name='%(nfsserver)s'/>
            <dir path='%(nfspath)s'/>
          </source>
          <target>
            <path>%(path)s</path>
          </target>
        </pool>
        """
    elif kwargs['type'] == 'logical':
        path = '/var/lib/kimchi/logical_mount/' + kwargs['name'];
        if not os.path.exists(path):
           os.makedirs(path)
        xml = """
        <pool type='%(type)s'>
        <name>%(name)s</name>
            <source>
            %(devices)s
            </source>
        <target>
            <path>%(path)s</path>
        </target>
        </pool>
        """
        devices = ''
        for device_path in kwargs['devices']:
            devices += "<device path=\""+device_path+"\" />"

        kwargs.update({'devices': devices})
        kwargs.update({'path': path})
    return xml % kwargs

def _get_volume_xml(**kwargs):
    # Required parameters
    # name:
    # capacity:
    #
    # Optional:
    # allocation:
    # format:
    kwargs.setdefault('allocation', 0)
    kwargs.setdefault('format', 'qcow2')

    xml = """
    <volume>
      <name>%(name)s</name>
      <allocation unit="MiB">%(allocation)s</allocation>
      <capacity unit="MiB">%(capacity)s</capacity>
      <source>
      </source>
      <target>
        <format type='%(format)s'/>
      </target>
    </volume>
    """ % kwargs
    return xml


class LibvirtConnection(object):
    def __init__(self, uri):
        self.uri = uri
        self._connections = {}
        self._connectionLock = threading.Lock()
        self.wrappables = self.get_wrappable_objects()

    def get_wrappable_objects(self):
        """
        When a wrapped function returns an instance of another libvirt object,
        we also want to wrap that object so we can catch errors that happen when
        calling its methods.
        """
        objs = []
        for name in ('virDomain', 'virDomainSnapshot', 'virInterface',
                     'virNWFilter', 'virNetwork', 'virNodeDevice', 'virSecret',
                     'virStoragePool', 'virStorageVol', 'virStream'):
            try:
                attr = getattr(libvirt, name)
            except AttributeError:
                pass
            objs.append(attr)
        return tuple(objs)

    def get(self, conn_id=0):
        """
        Return current connection to libvirt or open a new one.  Wrap all
        callable libvirt methods so we can catch connection errors and handle
        them by restarting the server.
        """
        def wrapMethod(f):
            def wrapper(*args, **kwargs):
                try:
                    ret = f(*args, **kwargs)
                    return ret
                except libvirt.libvirtError as e:
                    edom = e.get_error_domain()
                    ecode = e.get_error_code()
                    EDOMAINS = (libvirt.VIR_FROM_REMOTE,
                                libvirt.VIR_FROM_RPC)
                    ECODES = (libvirt.VIR_ERR_SYSTEM_ERROR,
                              libvirt.VIR_ERR_INTERNAL_ERROR,
                              libvirt.VIR_ERR_NO_CONNECT,
                              libvirt.VIR_ERR_INVALID_CONN)
                    if edom in EDOMAINS and ecode in ECODES:
                        kimchi_log.error('Connection to libvirt broken. '
                                         'Recycling. ecode: %d edom: %d' %
                                         (ecode, edom))
                        with self._connectionLock:
                            self._connections[conn_id] = None
                    raise
            wrapper.__name__ = f.__name__
            wrapper.__doc__ = f.__doc__
            return wrapper

        with self._connectionLock:
            conn = self._connections.get(conn_id)
            if not conn:
                retries = 5
                while True:
                    retries = retries - 1
                    try:
                        conn = libvirt.open(self.uri)
                        break
                    except libvirt.libvirtError:
                        kimchi_log.error('Unable to connect to libvirt.')
                        if not retries:
                            kimchi_log.error('Libvirt is not available, exiting.')
                            cherrypy.engine.stop()
                            raise
                    time.sleep(2)

                for name in dir(libvirt.virConnect):
                    method = getattr(conn, name)
                    if callable(method) and not name.startswith('_'):
                        setattr(conn, name, wrapMethod(method))

                for cls in self.wrappables:
                    for name in dir(cls):
                        method = getattr(cls, name)
                        if callable(method) and not name.startswith('_'):
                            setattr(cls, name, wrapMethod(method))

                self._connections[conn_id] = conn
                # In case we're running into troubles with keeping the connections
                # alive we should place here:
                # conn.setKeepAlive(interval=5, count=3)
                # However the values need to be considered wisely to not affect
                # hosts which are hosting a lot of virtual machines
            return conn