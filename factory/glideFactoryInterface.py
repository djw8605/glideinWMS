#
# Project:
#   glideinWMS
#
# File Version: 
#
# Description:
#   This module implements the functions needed to advertize
#   and get commands from the Collector
#
# Author:
#   Igor Sfiligoi (Sept 7th 2006)
#

import condorExe
import condorMonitor
import condorManager
import os
import time
import string
import logSupport
import fcntl

############################################################
#
# Configuration
#
############################################################

#class FakeLog:
#    def write(self,str):
#        pass

class FactoryConfig:
    def __init__(self):
        # set default values
        # user should modify if needed

        # The name of the attribute that identifies the glidein
        self.factory_id = "glidefactory"
        self.client_id = "glideclient"
        self.factoryclient_id = "glidefactoryclient"
        self.factory_global = 'glidefactoryglobal'

        #Default the glideinWMS version string
        self.glideinwms_version = "glideinWMS UNKNOWN"

        # String to prefix for the attributes
        self.glidein_attr_prefix = ""

        # String to prefix for the parameters
        self.glidein_param_prefix = "GlideinParam"
        self.encrypted_param_prefix = "GlideinEncParam"

        # String to prefix for the monitoring
        self.glidein_monitor_prefix = "GlideinMonitor"

        # String to prefix for the requests
        self.client_req_prefix = "Req"

        # String to prefix for the web passing
        self.client_web_prefix = "Web"

        # The name of the signtype
        self.factory_signtype_id = "SupportedSignTypes"
        self.client_web_signtype_suffix = "SignType"

        # Should we use TCP for condor_advertise?
        self.advertise_use_tcp = False
        # Should we use the new -multiple for condor_advertise?
        self.advertise_use_multi = False


        # warning log files
        # default is FakeLog, any other value must implement the write(str) method
        #self.warning_log = FakeLog()

        # Location of lock directory
        self.lock_dir = "."


# global configuration of the module
factoryConfig = FactoryConfig()

#####################################################
# Exception thrown when multiple executions are used
# Helps handle partial failures

class MultiExeError(condorExe.ExeError):
    def __init__(self, arr): # arr is a list of ExeError exceptions
        self.arr = arr

        # First approximation of implementation, can be improved
        str_arr = []
        for e in arr:
            str_arr.append('%s' % e)
        
        error_str = '\\n'.join(str_arr)
        
        condorExe.ExeError.__init__(self, error_str)

############################################################
#
# User functions
#
############################################################


def findWork(factory_name, glidein_name, entry_name,
             supported_signtypes,
             pub_key_obj=None,
             additional_constraints=None):
    """
    Find request classAds that have my (factory, glidein name, entry name) and create the dictionary of work request information.

    @type factory_name: string
    @param factory_name: name of the factory
    @type glidein_name: string
    @param glidein_name: name of the glidein instance
    @type entry_name: string
    @param entry_name: name of the factory entry
    @type supported_signtypes: list
    @param supported_signtypes: only support one kind of signtype, 'sha1', default is None
    @type pub_key_obj: string
    @param pub_key_obj: only support 'RSA'
    @type additional_constraints: string
    @param additional_constraints: any additional constraints to include for querying the WMS collector, default is None
    
    @return: dictionary, each key is the name of a frontend.  Each value has a 'requests' and a 'params' key.  Both refer to classAd dictionaries.
        
    """
    
    global factoryConfig
    logSupport.log.debug("Querying collector for requests")
    
    status_constraint = '(GlideinMyType=?="%s") && (ReqGlidein=?="%s@%s@%s")' % (factoryConfig.client_id, entry_name, glidein_name, factory_name)

    if supported_signtypes != None:
        status_constraint += ' && stringListMember(%s%s,"%s")' % (factoryConfig.client_web_prefix, factoryConfig.client_web_signtype_suffix, string.join(supported_signtypes, ","))

    if additional_constraints != None:
        status_constraint = "((%s)&&(%s))" % (status_constraint, additional_constraints)
    
    status = condorMonitor.CondorStatus("any")
    status.require_integrity(True) #important, this dictates what gets submitted
    status.glidein_name = glidein_name
    status.entry_name = entry_name

    # serialize access to the Collector accross all the processes
    # these is a single Collector anyhow
    lock_fname=os.path.join(factoryConfig.lock_dir,"gfi_status.lock")
    if not os.path.exists(lock_fname): #create a lock file if needed
        try:
            fd=open(lock_fname,"w")
            fd.close()
        except:
            # could be a race condition
            pass
    
    fd=open(lock_fname,"r+")
    try:
        fcntl.flock(fd,fcntl.LOCK_EX)
        try:
            status.load(status_constraint)
        finally:
            fcntl.flock(fd,fcntl.LOCK_UN)
    finally:
        fd.close()

    data = status.fetchStored()

    reserved_names = ("ReqName", "ReqGlidein", "ClientName", "FrontendName", "GroupName", "ReqPubKeyID", "ReqEncKeyCode", "ReqEncIdentity", "AuthenticatedIdentity")

    out = {}

    # copy over requests and parameters
    for k in data.keys():
        kel = data[k]
        el = {"requests":{}, "web":{}, "params":{}, "params_decrypted":{}, "monitor":{}, "internals":{}}
        for (key, prefix) in (("requests", factoryConfig.client_req_prefix),
                             ("web", factoryConfig.client_web_prefix),
                             ("params", factoryConfig.glidein_param_prefix),
                             ("monitor", factoryConfig.glidein_monitor_prefix)):
            plen = len(prefix)
            for attr in kel.keys():
                if attr in reserved_names:
                    continue # skip reserved names
                if attr[:plen] == prefix:
                    el[key][attr[plen:]] = kel[attr]
        if pub_key_obj != None:
            if kel.has_key('ReqPubKeyID'):
                try:
                    sym_key_obj = pub_key_obj.extract_sym_key(kel['ReqEncKeyCode'])
                except:
                    continue # bad key, ignore entry
            else:
                sym_key_obj = None # no key used, will not decrypt
        else:
            sym_key_obj = None # have no key, will not decrypt

        if sym_key_obj != None:
            # this is verifying that the identity that the client claims to be is the identity that Condor thinks it is
            try:
                enc_identity = sym_key_obj.decrypt_hex(kel['ReqEncIdentity'])
            except:
                logSupport.log.warning("Client %s provided invalid ReqEncIdentity, could not decode. Skipping for security reasons." % k)
                continue # corrupted classad
            if enc_identity != kel['AuthenticatedIdentity']:
                logSupport.log.warning("Client %s provided invalid ReqEncIdentity(%s!=%s). Skipping for security reasons." % (k, enc_identity, kel['AuthenticatedIdentity']))
                continue # uh oh... either the client is misconfigured, or someone is trying to cheat
            

        invalid_classad = False
        for (key, prefix) in (("params_decrypted", factoryConfig.encrypted_param_prefix),):
            plen = len(prefix)
            for attr in kel.keys():
                if attr in reserved_names:
                    continue # skip reserved names
                if attr[:plen] == prefix:
                    el[key][attr[plen:]] = None # define it even if I don't understand the content
                    if sym_key_obj != None:
                        try:
                            el[key][attr[plen:]] = sym_key_obj.decrypt_hex(kel[attr])
                        except:
                            invalid_classad = True
                            break # I don't understand it -> invalid
        if invalid_classad:
            logSupport.log.warning("At least one of the encrypted parameters for client %s cannot be decoded. Skipping for security reasons." % k)
            continue # need to go this way as I may have problems in an inner loop


        for attr in kel.keys():
            if attr in ("ClientName", "FrontendName", "GroupName", "ReqName", "LastHeardFrom", "ReqPubKeyID", "AuthenticatedIdentity"):
                el["internals"][attr] = kel[attr]
        
        out[k] = el

    return out


############################################################

#
# Define global variables that keep track of the Daemon lifetime
#
start_time = time.time()
advertizeGlideinCounter = 0
advertizeGlobalCounter = 0
advertizeGFCCounter = {}

# glidein_attrs is a dictionary of values to publish
#  like {"Arch":"INTEL","MinDisk":200000}
# similar for glidein_params and glidein_monitor_monitors
def advertizeGlidein(factory_name, glidein_name, entry_name, trust_domain, auth_method,
                     supported_signtypes, pub_key_obj,
                     glidein_attrs={}, glidein_params={}, glidein_monitors={}):
    
    """
    Creates the glideclient classad and advertises.
    
    @type factory_name: string
    @param factory_name: the name of the factory
    @type glidein_name: string
    @param glidein_name: name of the glidein
    @type entry_name: string
    @param entry_name: name of the entry
    @type trust_domain: string
    @param trust_domain: trust domain for this entry
    @type auth_method: string
    @param auth_method: the authentication methods this entry supports in glidein submission, i.e. grid_proxy
    @type supported_signtypes: string
    @param supported_signtypes: suppported sign types, i.e. sha1
    @type glidein_attrs: dict 
    @param glidein_attrs: glidein attrs to be published, not be overwritten by Frontends
    @type glidein_params: dict 
    @param glidein_params: params to be published, can be overwritten by Frontends
    @type glidein_monitors: dict 
    @param glidein_monitors: monitor attrs to be published
    @type pub_key_obj: GlideinKey
    @param pub_key_obj: for the frontend to use in encryption
    """
    global factoryConfig, advertizeGlideinCounter

    # get a 9 digit number that will stay 9 digit for the next 25 years
    short_time = time.time() - 1.05e9
    tmpnam = "/tmp/gfi_ag_%li_%li" % (short_time, os.getpid())
    fd = file(tmpnam, "w")
    try:
        try:
            fd.write('MyType = "%s"\n' % factoryConfig.factory_id)
            fd.write('GlideinMyType = "%s"\n' % factoryConfig.factory_id)
            fd.write('GlideinWMSVersion = "%s"\n' % factoryConfig.glideinwms_version)
            fd.write('Name = "%s@%s@%s"\n' % (entry_name, glidein_name, factory_name))
            fd.write('FactoryName = "%s"\n' % factory_name)
            fd.write('GlideinName = "%s"\n' % glidein_name)
            fd.write('EntryName = "%s"\n' % entry_name)
            fd.write('%s = "%s"\n' % (factoryConfig.factory_signtype_id, string.join(supported_signtypes, ',')))
            # Must have a key to communicate
            fd.write('PubKeyID = "%s"\n' % pub_key_obj.get_pub_key_id())
            fd.write('PubKeyType = "%s"\n' % pub_key_obj.get_pub_key_type())
            fd.write('PubKeyValue = "%s"\n' % string.replace(pub_key_obj.get_pub_key_value(), '\n', '\\n'))
            if 'grid_proxy' in auth_method:
                fd.write('GlideinAllowx509_Proxy = %s\n' % True)
                fd.write('GlideinRequirex509_Proxy = %s\n' % True)
                fd.write('GlideinRequireGlideinProxy = %s\n' % False)
            else:
                fd.write('GlideinAllowx509_Proxy = %s\n' % False)
                fd.write('GlideinRequirex509_Proxy = %s\n' % False)
                fd.write('GlideinRequireGlideinProxy = %s\n' % True)
            fd.write('DaemonStartTime = %li\n' % start_time)
            fd.write('UpdateSequenceNumber = %i\n' % advertizeGlideinCounter)
            advertizeGlideinCounter += 1

            # write out both the attributes, params and monitors
            for (prefix, data) in ((factoryConfig.glidein_attr_prefix, glidein_attrs),
                                  (factoryConfig.glidein_param_prefix, glidein_params),
                                  (factoryConfig.glidein_monitor_prefix, glidein_monitors)):
                for attr in data.keys():
                    el = data[attr]
                    if type(el) == type(1):
                        # don't quote ints
                        fd.write('%s%s = %s\n' % (prefix, attr, el))
                    else:
                        escaped_el = string.replace(string.replace(str(el), '"', '\\"'), '\n', '\\n')
                        fd.write('%s%s = "%s"\n' % (prefix, attr, escaped_el))
        finally:
            fd.close()

        exe_condor_advertise(tmpnam, "UPDATE_MASTER_AD")
    finally:
        os.remove(tmpnam)

def advertizeGlobal(factory_name, glidein_name, supported_signtypes, pub_key_obj):
    
    """
    Creates the glidefactoryglobal classad and advertises.
    
    @type factory_name: string
    @param factory_name: the name of the factory
    @type glidein_name: string
    @param glidein_name: name of the glidein
    @type supported_signtypes: string
    @param supported_signtypes: suppported sign types, i.e. sha1
    @type pub_key_obj: GlideinKey
    @param pub_key_obj: for the frontend to use in encryption
    
    @todo add factory downtime?
    """
    
    global factoryConfig
    global advertizeGlobalCounter

    # get a 9 digit number that will stay 9 digit for the next 25 years
    short_time = time.time() - 1.05e9
    tmpnam = "/tmp/gfi_ag_%li_%li" % (short_time, os.getpid())
    fd = file(tmpnam, "w")

    try:
        try:
            fd.write('MyType = "%s"\n' % factoryConfig.factory_global)
            fd.write('GlideinMyType = "%s"\n' % factoryConfig.factory_global)
            fd.write('GlideinWMSVersion = "%s"\n' % factoryConfig.glideinwms_version)
            fd.write('Name = "%s@%s"\n' % (glidein_name, factory_name))
            fd.write('FactoryName = "%s"\n' % factory_name)
            fd.write('GlideinName = "%s"\n' % glidein_name)
            fd.write('%s = "%s"\n' % (factoryConfig.factory_signtype_id, string.join(supported_signtypes, ',')))
            fd.write('PubKeyID = "%s"\n' % pub_key_obj.get_pub_key_id())
            fd.write('PubKeyType = "%s"\n' % pub_key_obj.get_pub_key_type())
            fd.write('PubKeyValue = "%s"\n' % string.replace(pub_key_obj.get_pub_key_value(), '\n', '\\n'))
            fd.write('DaemonStartTime = %li\n' % start_time)
            fd.write('UpdateSequenceNumber = %i\n' % advertizeGlobalCounter)
            advertizeGlobalCounter += 1
        finally:
            fd.close()
            
        exe_condor_advertise(tmpnam, "UPDATE_MASTER_AD")
               
    finally:
        os.remove(tmpnam)


def deadvertizeGlidein(factory_name, glidein_name, entry_name):
    """
    Removes the glidefactory classad advertising the entry from the WMS Collector.
    """
    # get a 9 digit number that will stay 9 digit for the next 25 years
    short_time = time.time() - 1.05e9
    tmpnam = "/tmp/gfi_ag_%li_%li" % (short_time, os.getpid())
    fd = file(tmpnam, "w")
    try:
        try:
            fd.write('MyType = "Query"\n')
            fd.write('TargetType = "%s"\n' % factoryConfig.factory_id)
            fd.write('Requirements = (Name == "%s@%s@%s")&&(GlideinMyType == "%s")\n' % (entry_name, glidein_name, factory_name, factoryConfig.factory_id))
        finally:
            fd.close()

        exe_condor_advertise(tmpnam, "INVALIDATE_MASTER_ADS")
    finally:
        os.remove(tmpnam)

        
def deadvertizeGlobal(factory_name, glidein_name):
    """
    Removes the glidefactoryglobal classad advertising the factory globals from the WMS Collector.
    """
    # get a 9 digit number that will stay 9 digit for the next 25 years
    short_time = time.time() - 1.05e9
    tmpnam = "/tmp/gfi_ag_%li_%li" % (short_time, os.getpid())
    fd = file(tmpnam, "w")
    try:
        try:
            fd.write('MyType = "Query"\n')
            fd.write('TargetType = "%s"\n' % factoryConfig.factory_global)
            fd.write('Requirements = (Name == "%s@%s")&&(GlideinMyType == "%s")\n' % (glidein_name, factory_name, factoryConfig.factory_id))
        finally:
            fd.close()

        exe_condor_advertise(tmpnam, "INVALIDATE_MASTER_ADS")
    finally:
        os.remove(tmpnam)

def deadvertizeFactory(factory_name, glidein_name):
    """
    Deadvertize all entry and global classads for this factory.
    """
    # get a 9 digit number that will stay 9 digit for the next 25 years
    short_time = time.time() - 1.05e9
    tmpnam = "/tmp/gfi_ag_%li_%li" % (short_time, os.getpid())
    fd = file(tmpnam, "w")
    try:
        try:
            fd.write('MyType = "Query"\n')
            fd.write('TargetType = "%s"\n' % factoryConfig.factory_id)
            fd.write('Requirements = (FactoryName =?= "%s")&&(GlideinName =?= "%s")\n' % (factory_name, glidein_name))
        finally:
            fd.close()

        exe_condor_advertise(tmpnam, "INVALIDATE_MASTER_ADS")
    finally:
        os.remove(tmpnam)
    

############################################################

# glidein_attrs is a dictionary of values to publish
#  like {"Arch":"INTEL","MinDisk":200000}
# similar for glidein_params and glidein_monitor_monitors
def advertizeGlideinClientMonitoring(factory_name, glidein_name, entry_name,
                                     client_name, client_int_name, client_int_req,
                                     glidein_attrs={}, client_params={}, client_monitors={}):
    # get a 9 digit number that will stay 9 digit for the next 25 years
    short_time = time.time() - 1.05e9
    tmpnam = "/tmp/gfi_agcm_%li_%li" % (short_time, os.getpid())

    createGlideinClientMonitoringFile(tmpnam, factory_name, glidein_name, entry_name,
                                      client_name, client_int_name, client_int_req,
                                      glidein_attrs, client_params, client_monitors)
    advertizeGlideinClientMonitoringFromFile(tmpnam, remove_file=True)

class MultiAdvertizeGlideinClientMonitoring:
    # glidein_attrs is a dictionary of values to publish
    #  like {"Arch":"INTEL","MinDisk":200000}
    def __init__(self,
                 factory_name, glidein_name, entry_name,
                 glidein_attrs):
        self.factory_name = factory_name
        self.glidein_name = glidein_name
        self.entry_name = entry_name
        self.glidein_attrs = glidein_attrs
        self.client_data = []

    def add(self,
            client_name, client_int_name, client_int_req,
            client_params={}, client_monitors={}):
        el = {'client_name':client_name,
            'client_int_name':client_int_name,
            'client_int_req':client_int_req,
            'client_params':client_params,
            'client_monitors':client_monitors}
        self.client_data.append(el)

    # do the actual advertizing
    # can throw MultiExeError
    def do_advertize(self):
        if factoryConfig.advertise_use_multi:
            self.do_advertize_multi()
        else:
            self.do_advertize_iterate()
        self.client_data = []

        
    # INTERNAL
    def do_advertize_iterate(self):
        error_arr = []

        # get a 9 digit number that will stay 9 digit for the next 25 years
        short_time = time.time() - 1.05e9
        tmpnam = "/tmp/gfi_agcm_%li_%li" % (short_time, os.getpid())

        for el in self.client_data:
            createGlideinClientMonitoringFile(tmpnam, self.factory_name, self.glidein_name, self.entry_name,
                                              el['client_name'], el['client_int_name'], el['client_int_req'],
                                              self.glidein_attrs, el['client_params'], el['client_monitors'])
            try:
                advertizeGlideinClientMonitoringFromFile(tmpnam, remove_file=True)
            except condorExe.ExeError, e:
                error_arr.append(e)

        if len(error_arr) > 0:
            raise MultiExeError, error_arr
        
    def do_advertize_multi(self):
        # get a 9 digit number that will stay 9 digit for the next 25 years
        short_time = time.time() - 1.05e9
        tmpnam = "/tmp/gfi_agcm_%li_%li" % (short_time, os.getpid())

        ap = False
        for el in self.client_data:
            createGlideinClientMonitoringFile(tmpnam, self.factory_name, self.glidein_name, self.entry_name,
                                              el['client_name'], el['client_int_name'], el['client_int_req'],
                                              self.glidein_attrs, el['client_params'], el['client_monitors'],
                                              do_append=ap)
            ap = True # Append from here on

        if ap:
            error_arr = []
            try:
                advertizeGlideinClientMonitoringFromFile(tmpnam, remove_file=True, is_multi=True)
            except condorExe.ExeError, e:
                error_arr.append(e)

            if len(error_arr) > 0:
                raise MultiExeError, error_arr
        


##############################
# Start INTERNAL

# glidein_attrs is a dictionary of values to publish
#  like {"Arch":"INTEL","MinDisk":200000}
# similar for glidein_params and glidein_monitor_monitors
def createGlideinClientMonitoringFile(fname,
                                      factory_name, glidein_name, entry_name,
                                      client_name, client_int_name, client_int_req,
                                      glidein_attrs={}, client_params={}, client_monitors={},
                                      do_append=False):
    global factoryConfig
    global advertizeGFCCounter

    if do_append:
        open_type = "a"
    else:
        open_type = "w"
        
    fd = file(fname, open_type)
    try:
        try:
            fd.write('MyType = "%s"\n' % factoryConfig.factoryclient_id)
            fd.write('GlideinMyType = "%s"\n' % factoryConfig.factoryclient_id)
            fd.write('GlideinWMSVersion = "%s"\n' % factoryConfig.glideinwms_version)
            fd.write('Name = "%s"\n' % client_name)
            fd.write('ReqGlidein = "%s@%s@%s"\n' % (entry_name, glidein_name, factory_name))
            fd.write('ReqFactoryName = "%s"\n' % factory_name)
            fd.write('ReqGlideinName = "%s"\n' % glidein_name)
            fd.write('ReqEntryName = "%s"\n' % entry_name)
            fd.write('ReqClientName = "%s"\n' % client_int_name)
            fd.write('ReqClientReqName = "%s"\n' % client_int_req)
            #fd.write('DaemonStartTime = %li\n'%start_time)
            if advertizeGFCCounter.has_key(client_name):
                advertizeGFCCounter[client_name] += 1
            else:
                advertizeGFCCounter[client_name] = 0
            fd.write('UpdateSequenceNumber = %i\n'%advertizeGFCCounter[client_name])            

            # write out both the attributes, params and monitors
            for (prefix, data) in ((factoryConfig.glidein_attr_prefix, glidein_attrs),
                                  (factoryConfig.glidein_param_prefix, client_params),
                                  (factoryConfig.glidein_monitor_prefix, client_monitors)):
                for attr in data.keys():
                    el = data[attr]
                    if type(el) == type(1):
                        # don't quote ints
                        fd.write('%s%s = %s\n' % (prefix, attr, el))
                    else:
                        escaped_el = string.replace(str(el), '"', '\\"')
                        fd.write('%s%s = "%s"\n' % (prefix, attr, escaped_el))
            # add a final empty line... useful when appending
            fd.write('\n')
        finally:
            fd.close()
    except:
        # remove file in case of problems
        os.remove(fname)
        raise

# Given a file, advertize
# Can throw a CondorExe/ExeError exception
def advertizeGlideinClientMonitoringFromFile(fname,
                                             remove_file=True,
                                             is_multi=False):
    try:
        exe_condor_advertise(fname, "UPDATE_LICENSE_AD", is_multi=is_multi)
    finally:
        if remove_file:
            os.remove(fname)

# End INTERNAL
###########################################

# remove adds from Collector
def deadvertizeAllGlideinClientMonitoring(factory_name, glidein_name, entry_name):
    """
    Deadvertize  monitoring classads for the given entry.
    """
    # get a 9 digit number that will stay 9 digit for the next 25 years
    short_time = time.time() - 1.05e9
    tmpnam = "/tmp/gfi_ag_%li_%li" % (short_time, os.getpid())
    fd = file(tmpnam, "w")
    try:
        try:
            fd.write('MyType = "Query"\n')
            fd.write('TargetType = "%s"\n' % factoryConfig.factoryclient_id)
            fd.write('Requirements = (ReqGlidein == "%s@%s@%s")&&(GlideinMyType == "%s")\n' % (entry_name, glidein_name, factory_name, factoryConfig.factoryclient_id))
        finally:
            fd.close()

        exe_condor_advertise(tmpnam, "INVALIDATE_LICENSE_ADS")
    finally:
        os.remove(tmpnam)


def deadvertizeFactoryClientMonitoring(factory_name, glidein_name):
    """
    Deadvertize all monitoring classads for this factory.
    """
    # get a 9 digit number that will stay 9 digit for the next 25 years
    short_time = time.time() - 1.05e9
    tmpnam = "/tmp/gfi_ag_%li_%li" % (short_time, os.getpid())
    fd = file(tmpnam, "w")
    try:
        try:
            fd.write('MyType = "Query"\n')
            fd.write('TargetType = "%s"\n' % factoryConfig.factoryclient_id)
            fd.write('Requirements = (ReqFactoryName=?="%s")&&(ReqGlideinName=?="%s")&&(GlideinMyType == "%s")' % (factory_name, glidein_name, factoryConfig.factoryclient_id))
        finally:
            fd.close()

        exe_condor_advertise(tmpnam, "INVALIDATE_LICENSE_ADS")
    finally:
        os.remove(tmpnam)
    
############################################################
#
# I N T E R N A L - Do not use
#
############################################################

# serialize access to the Collector accross all the processes
# these is a single Collector anyhow
def exe_condor_advertise(fname, command,
                         is_multi=False):
    global factoryConfig

    lock_fname=os.path.join(factoryConfig.lock_dir,"gfi_advertize.lock")
    if not os.path.exists(lock_fname): #create a lock file if needed
        try:
            fd=open(lock_fname,"w")
            fd.close()
        except:
            # could be a race condition
            pass
    
    fd=open(lock_fname,"r+")
    try:
        fcntl.flock(fd,fcntl.LOCK_EX)
        try:
            ret = condorManager.condorAdvertise(fname, command, factoryConfig.advertise_use_tcp, is_multi)
        finally:
            fcntl.flock(fd,fcntl.LOCK_UN)
    finally:
        fd.close()

    return ret
    

