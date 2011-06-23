#!/usr/bin/env python
#
# Project:
#   glideinWMS
#
# File Version:
#   $Id: glideFactoryEntry.py,v 1.96.2.24.2.25 2011/06/23 14:43:56 klarson1 Exp $
#
# Description:
#   This is the main of the glideinFactoryEntry
#
# Arguments:
#   $1 = poll period (in seconds)
#   $2 = advertize rate (every $2 loops)
#   $3 = glidein submit_dir
#   $4 = entry name
#
# Author:
#   Igor Sfiligoi (Sep 15th 2006 - as glideFactory.py)
#   Igor Sfiligoi (Apr 9th 2007 - as glideFactoryEntry.py)
#

import signal
import os
import sys
import traceback
import time
import string
import math
import copy
import random
import sets
import logging
sys.path.append(os.path.join(sys.path[0], "../lib"))

import glideFactoryPidLib
import glideFactoryConfig
import glideFactoryLib
import glideFactoryMonitoring
import glideFactoryInterface
import glideFactoryLogParser
import glideFactoryDowntimeLib
import glideinWMSVersion

import logSupport
import cleanupSupport

# This declaration is not strictly needed - it is declared as global in main
# however, to make code clearer (hopefully), it is declared here to make it
# easy to see that log is a module level variable
log = None

############################################################
def check_parent(parent_pid, glideinDescript, jobDescript):
    """Check to make sure that we aren't an orphaned process.  If Factory
    daemon has died, then clean up after ourselves and kill ourselves off.

    @type parent_pid: int
    @param parent_pid: the pid for the Factory daemon
    @type glideinDescript: glideFactoryConfig.GlideinDescript
    @param glideinDescript: Object that encapsulates glidein.descript in the Factory root directory
    @type jobDescript: glideFactoryConfig.JobDescript
    @param jobDescript: Object that encapsulates job.descript in the entry directory

    @raise KeyboardInterrupt: Raised when the Factory daemon cannot be found
    """
    if os.path.exists('/proc/%s' % parent_pid):
        return # parent still exists, we are fine

    logSupport.log.info("Parent died, exit.")

    # there is nobody to clean up after ourselves... do it here
    logSupport.log.info("Deadvertize myself")

    # Attempt to deadvertise the entry classads
    try:
        glideFactoryInterface.deadvertizeGlidein(glideinDescript.data['FactoryName'],
                                                 glideinDescript.data['GlideinName'],
                                                 jobDescript.data['EntryName'])
    except:
        logSupport.log.warning("Failed to deadvertize myself")

    # Attempt to deadvertise the entry monitoring classads
    try:
        glideFactoryInterface.deadvertizeAllGlideinClientMonitoring(glideinDescript.data['FactoryName'],
                                                                    glideinDescript.data['GlideinName'],
                                                                    jobDescript.data['EntryName'])
    except:
        logSupport.log.warning("Failed to deadvertize my monitoring")
    
    try:
        glideFactoryInterface.deadvertizeGlobal(glideinDescript.data['FactoryName'],     
                                                 glideinDescript.data['GlideinName'])     
    except:
        logSupport.log.warning("Failed to deadvertize my global")

    raise KeyboardInterrupt, "Parent died"


############################################################
def perform_work(entry_name, condorQ,
                 client_name, client_int_name, client_security_name,
                 credential_security_class, client_int_req,
                 in_downtime, remove_excess,
                 idle_glideins, max_running, max_held,
                 jobDescript, x509_proxy_fnames, credential_username,
                 identity_credentials,
                 client_web, params):
    """
    Logs stats.  Determines how many idle glideins are needed per proxy.

    @type entry_name:  string
    @param entry_name:  name of the entry
    @type condorQ:  CondorQ object
    @param condorQ:  Condor queue filtered by security class
    @type client_name:  string
    @param client_name:  name of the frontend client
    @type client_int_name:  string
    @param client_int_name:  client name in the request
    @type client_security_name:  string
    @param client_security_name:  decrypted client security name in the request
    @type credential_security_class:  string
    @param credential_security_class:  security class this client and credential are mapped to
    @type client_int_req:  string
    @param client_int_req:  name of the request from the client
    @type in_downtime:  boolean
    @param in_downtime:  true if entry (or factory) is in downtime
    @type remove_excess:  string
    @param remove_excess: contains the value specified in the request.  If none specified, contains 'NO'
    @type idle_glideins:  int
    @param idle_glideins:  number of idle glideins requested
    @type max_running:  int
    @param max_running:  max number of running glideins requested
    @type max_held:  int
    @param max_held:  max number of held glideins requested
    @type jobDescript:  dict
    @param jobDescript:  entry configuration values
    @type x509_proxy_fnames:  dict
    @param x509_proxy_fnames:  proxies for the associated security class
    @type credential_username:  string
    @param credential_username:  username to be used for submitting glideins (if not factory username, privsep is used)
    @type identity_credentials: dict
    @param identity_credentials: identity information passed by the frontend
    @type client_web:   glideFactoryLib.ClientWeb or glideFactoryLib.ClientWebNoGroup
    @param client_web:  client web values
    @type params:  dict
    @param params:  entry parameters to be passed to the glidein
    """

    glideFactoryLib.factoryConfig.client_internals[client_int_name] = {"CompleteName":client_name, "ReqName":client_int_req}

    #if params.has_key("GLIDEIN_Collector"):
    #    condor_pool = params["GLIDEIN_Collector"]
    #else:
    #    condor_pool = None

    # not used in original code but need to pass it to the functions anyway
    condorStatus = None

    x509_proxy_keys = x509_proxy_fnames.keys()
    random.shuffle(x509_proxy_keys) # randomize so I don't favour any proxy over another

    # find out the users it is using
    log_stats = {}
    log_stats[credential_username] = glideFactoryLogParser.dirSummaryTimingsOut(glideFactoryLib.factoryConfig.get_client_log_dir(entry_name, credential_username),
                                                                              logSupport.log_dir, client_int_name, credential_username)
    # should not need privsep for reading logs
    log_stats[credential_username].load()

    glideFactoryLib.logStats(condorQ, condorStatus, client_int_name, client_security_name, credential_security_class)
    client_log_name = glideFactoryLib.secClass2Name(client_security_name, credential_security_class)
    glideFactoryLib.factoryConfig.log_stats.logSummary(client_log_name, log_stats) #@UndefinedVariable

    remove_excess_wait = False
    remove_excess_idle = False
    remove_excess_run = False
    if remove_excess == 'NO':
        pass # use defaults
    elif remove_excess == 'WAIT':
        remove_excess_wait = True
    elif remove_excess == 'IDLE':
        remove_excess_wait = True
        remove_excess_idle = True
    elif remove_excess == 'ALL':
        remove_excess_wait = True
        remove_excess_idle = True
        remove_excess_run = True
    else:
        # nothing to do
        logSupport.log.info("Unknown RemoveExcess '%s', assuming 'NO'" % remove_excess)

    # use the extended params for submission
    proxy_fraction = 1.0 / len(x509_proxy_keys)

    # I will shuffle proxies around, so I may as well round up all of them
    idle_glideins_pproxy = int(math.ceil(idle_glideins * proxy_fraction))
    max_running_pproxy = int(math.ceil(max_running * proxy_fraction))

    # not reducing the held, as that is effectively per proxy, not per request
    nr_submitted = 0
    for x509_proxy_id in x509_proxy_keys:
        security_credentials = {}
        security_credentials['SubmitProxy'] = x509_proxy_fnames[x509_proxy_id]
        submit_credentials = SubmitCredentials(credential_username, credential_security_class)
        submit_credentials.id = x509_proxy_id
        submit_credentials.security_credentials = security_credentials
        submit_credentials.identity_credentials = identity_credentials
        nr_submitted += glideFactoryLib.keepIdleGlideins(condorQ, client_int_name, in_downtime,
                                                         remove_excess_wait, remove_excess_idle, remove_excess_run,
                                                         idle_glideins_pproxy, max_running_pproxy, max_held,
                                                         #x509_proxy_id, x509_proxy_fnames[x509_proxy_id], credential_username, credential_security_class,
                                                         submit_credentials,
                                                         client_web, params)

    if nr_submitted > 0:
        return 1 # we submitted something, return immediately

    if condorStatus != None: # temporary glitch, no sanitization this round
        glideFactoryLib.sanitizeGlideins(condorQ, condorStatus)
    else:
        glideFactoryLib.sanitizeGlideinsSimple(condorQ)

    return 0

############################################################
# only allow simple strings
def is_str_safe(s):
    for c in s:
        if not c in ('._-@' + string.ascii_letters + string.digits):
            return False
    return True

############################################################
class X509Proxies:
    """
    I think this class is only used for the v2+ protocol
    """
    def __init__(self, frontendDescript, client_security_name):
        self.frontendDescript = frontendDescript
        self.client_security_name = client_security_name
        self.usernames = {}
        self.fnames = {}
        self.count_fnames = 0 # len of sum(fnames)

    # Return None, if cannot convert
    def get_username(self, x509_proxy_security_class):
        if not self.usernames.has_key(x509_proxy_security_class):
            # lookup only the first time
            x509_proxy_username = self.frontendDescript.get_username(self.client_security_name, x509_proxy_security_class)
            if x509_proxy_username == None:
                # but don't cache misses
                return None
            self.usernames[x509_proxy_security_class] = x509_proxy_username
        return self.usernames[x509_proxy_security_class][:]

    def add_fname(self, x509_proxy_security_class, x509_proxy_identifier, x509_proxy_fname):
        if not self.fnames.has_key(x509_proxy_security_class):
            self.fnames[x509_proxy_security_class] = {}
        self.fnames[x509_proxy_security_class][x509_proxy_identifier] = x509_proxy_fname
        self.count_fnames += 1

###
def find_and_perform_work(in_downtime, glideinDescript, frontendDescript, jobDescript, jobAttributes, jobParams):
    """
    Finds work requests from the WMS collector, validates security credentials, and requests glideins.  If an entry is
    in downtime, requested glideins is zero.

    @type in_downtime: boolean
    @param in_downtime: True if entry is in downtime
    @type glideinDescript:  dictionary
    @param glideinDescript: factory configuration values
    @type frontendDescript:  dictionary
    @param frontendDescript: frontend identity and security mappings
    @type jobDescript:  dictionary
    @param jobDescript: entry configuration values
    @type jobAttributes:  dictionary
    @param jobAttributes: entry attributes to be published
    @type jobParams:  dictionary
    @param jobParams: entry parameters that will be passed to the glideins

    @return: returns a value greater than zero if work was done.
    """

    # Get glidein and entry details
    schedd_name = jobDescript.data['Schedd']
    factory_max_running = int(jobDescript.data['MaxRunning'])
    factory_max_idle = int(jobDescript.data['MaxIdle'])
    factory_max_held = int(jobDescript.data['MaxHeld'])
    entry_name = jobDescript.data['EntryName']
    pub_key_obj = glideinDescript.data['PubKeyObj']
    auth_method = jobDescript.data['AuthMethod']
    glidein_name = glideFactoryLib.factoryConfig.glidein_name
    client_proxies_base_dir = glideFactoryLib.factoryConfig.client_proxies_base_dir

    # Get the factory and entry downtimes
    factory_downtimes = glideFactoryDowntimeLib.DowntimeFile(glideinDescript.data['DowntimesFile'])
    
    # Get information about which VOs to allow for this entry point.
    # This will be a comma-delimited list of pairs
    # vofrontendname:security_class,vofrontend:sec_class, ...
    frontend_whitelist = jobDescript.data['WhitelistMode']
    security_list = {}
    if (frontend_whitelist == "On"):
        frontend_allowed = jobDescript.data['AllowedVOs']
        frontend_allow_list = frontend_allowed.split(',')
        for entry in frontend_allow_list:
            entry_part = entry.split(":")
            if (security_list.has_key(entry_part[0])):
                security_list[entry_part[0]].append(entry_part[1])
            else:
                security_list[entry_part[0]] = [entry_part[1]]

    # Set downtime in the stats
    glideFactoryLib.factoryConfig.client_stats.set_downtime(in_downtime) 
    glideFactoryLib.factoryConfig.qc_stats.set_downtime(in_downtime) 

    # ===========  Finding work requests and queue data ==========
    logSupport.log.debug("Finding work")
    additional_constraints = None
    if pub_key_obj != None:
        # get only classads that have my key or no key at all, any other key will not work
        additional_constraints = '(((ReqPubKeyID=?="%s") && (ReqEncKeyCode=!=Undefined) && (ReqEncIdentity=!=Undefined)) || (ReqPubKeyID=?=Undefined))' % pub_key_obj.get_pub_key_id() 
    work = glideFactoryInterface.findWork(glideFactoryLib.factoryConfig.factory_name,
                                          glideFactoryLib.factoryConfig.glidein_name,
                                          entry_name,
                                          glideFactoryLib.factoryConfig.supported_signtypes,
                                          pub_key_obj,
                                          additional_constraints)
    
    if len(work.keys()) == 0:
        logSupport.log.debug("No work found")
        return 0 # nothing to be done

    try:
        logSupport.log.debug("Querying factory queue for glideins.")
        condorQ = glideFactoryLib.getCondorQData(entry_name, None, schedd_name)
    except glideFactoryLib.condorExe.ExeError, e:
        logSupport.log.info("Schedd %s not responding, skipping" % schedd_name)
        logSupport.log.warning("getCondorQData failed: %s" % e)
        # protect and exit
        return 0
    except:
        logSupport.log.info("Schedd %s not responding, skipping" % schedd_name)
        tb = traceback.format_exception(sys.exc_info()[0], sys.exc_info()[1], sys.exc_info()[2])
        logSupport.log.warning("getCondorQData failed, traceback: %s" % string.join(tb, ''))
        # protect and exit
        return 0

    # Initialize variables
    all_security_names = sets.Set()

    # ===== process work requests ============
    logSupport.log.debug("Validating requests and doing work")
    done_something = 0
    for work_key in work.keys():
    
        # Initialize credentials for each request
        security_credentials = {}      
        identity_credentials = {}  
        credential_security_class = None   
        credential_username = None     
        
        # Key name may be used to write files... make sure it is reasonable
        if not is_str_safe(work_key):
            logSupport.log.warning("Request name '%s' not safe. Skipping request" % work_key)
            continue #skip request

        # merge work and default params
        params = work[work_key]['params']
        decrypted_params = work[work_key]['params_decrypted']

        # add default values if not defined
        for k in jobParams.data.keys():
            if not (k in params.keys()):
                params[k] = jobParams.data[k]

        # Set client name (i.e. frontend.group) and request (i.e. entry@glidein@factory) names
        try:
            client_int_name = work[work_key]['internals']["ClientName"]
            client_int_req = work[work_key]['internals']["ReqName"]
        except:
            client_int_name = "DummyName"
            client_int_req = "DummyReq"
        if not is_str_safe(client_int_name):
            # may be used to write files... make sure it is reasonable
            logSupport.log.warning("Client name '%s' not safe. Skipping request" % client_int_name)
            continue #skip request
        
        # ======== validate security and whitelist information ================
        # Check whether the frontend is on the whitelist for the entry point
        if decrypted_params.has_key('SecurityName'):
            client_security_name = decrypted_params['SecurityName']
        else:
            logSupport.log.warning("Client %s did not provide th security name, skipping request" % client_int_name)
            continue

        if frontend_whitelist == "On" and not security_list.has_key(client_security_name):
            logSupport.log.warning("Client name '%s' not in whitelist. Preventing glideins from %s " % (client_security_name, client_int_name))
            in_downtime = True        
                      
        # Check if project id is required    
        if 'project_id' in auth_method:
            # Validate project id exists
            if decrypted_params.has_key('ProjectId'):
                project_id = decrypted_params['ProjectId']
                # just add to params for now, not a security issue
                params['ProjectId'] = project_id  # for v2+ protocol only?
                submit_credentials.add_identity_credential('ProjectId', project_id)
            else:
                # project id is required, cannot service request
                logSupport.log.info("Client '%s' did not specify a Project Id in the request, this is required by entry %s, skipping "%(client_int_name, jobDescript.data['EntryName']))
                continue  

        # ========= v2+ protocol ==============
        if decrypted_params.has_key('x509_proxy_0'):
            if not ('grid_proxy' in auth_method):
                logSupport.log.warning("Client %s provided proxy, but a client supplied proxy is not allowed. Skipping bad request" % client_int_name)
                continue #skip request
    
            client_expected_identity = frontendDescript.get_identity(client_security_name)
            if client_expected_identity == None:
                logSupport.log.warning("Client %s (secid: %s) not in white list. Skipping request" % (client_int_name, client_security_name))
                continue #skip request
    
            client_authenticated_identity = work[work_key]['internals']["AuthenticatedIdentity"]
    
            if client_authenticated_identity != client_expected_identity:
                # silently drop... like if we never read it in the first place
                # this is compatible with what the frontend does
                logSupport.log.warning("Client %s (secid: %s) is not coming from a trusted source; AuthenticatedIdentity %s!=%s. " \
                                "Skipping for security reasons." % (client_int_name, client_security_name,
                                                                    client_authenticated_identity, client_expected_identity))
                continue #skip request
                
            x509_proxies = X509Proxies(frontendDescript, client_security_name)
            if not decrypted_params.has_key('nr_x509_proxies'):
                logSupport.log.warning("Could not determine number of proxies for %s, skipping request" % client_int_name)
                continue #skip request
            try:
                nr_x509_proxies = int(decrypted_params['nr_x509_proxies'])
            except:
                logSupport.log.warning("Invalid number of proxies for %s, skipping request" % client_int_name)
                continue # skip request
                
            # If the whitelist mode is on, then set downtime to true
            # We will set it to false in the loop if a security class passes the test
            if frontend_whitelist == "On":
                prev_downtime = in_downtime
                in_downtime = True                    
            
            # Set security class downtime flag
            security_class_downtime_found = False
            
            # Validate each proxy
            for i in range(nr_x509_proxies):
                # Get proxy params 
                if decrypted_params['x509_proxy_%i' % i] == None:
                    logSupport.log.warning("Could not decrypt x509_proxy_%i for %s, skipping and trying the others" % (i, client_int_name))
                    continue #skip proxy
                if not decrypted_params.has_key('x509_proxy_%i_identifier' % i):
                    logSupport.log.warning("No identifier for x509_proxy_%i for %s, skipping and trying the others" % (i, client_int_name))
                    continue #skip proxy
                x509_proxy = decrypted_params['x509_proxy_%i' % i]
                x509_proxy_identifier = decrypted_params['x509_proxy_%i_identifier' % i]
    
                # Make sure proxy id is safe to write files... make sure it is reasonable
                if not is_str_safe(x509_proxy_identifier):
                    logSupport.log.warning("Identifier for x509_proxy_%i for %s is not safe ('%s), skipping and trying the others" % (i, client_int_name, x509_proxy_identifier))
                    continue #skip proxy
    
                # Check security class for downtime (in downtimes file)
                if decrypted_params.has_key('x509_proxy_%i_security_class' % i):
                    x509_proxy_security_class = decrypted_params['x509_proxy_%i_security_class' % i]
                else:
                    x509_proxy_security_class = x509_proxy_identifier
                logSupport.log.info("Checking downtime for frontend %s security class: %s (entry %s)." % (client_security_name, x509_proxy_security_class, jobDescript.data['EntryName']))
                in_sec_downtime = (factory_downtimes.checkDowntime(entry="factory", frontend=client_security_name, security_class=x509_proxy_security_class) or
                                       factory_downtimes.checkDowntime(entry=jobDescript.data['EntryName'], frontend=client_security_name, security_class=x509_proxy_security_class))
                if in_sec_downtime:
                    logSupport.log.warning("Security Class %s is currently in a downtime window for Entry: %s. Ignoring request." % (x509_proxy_security_class, jobDescript.data['EntryName']))
                    security_class_downtime_found = True
                    continue # cannot use proxy for submission but entry is not in downtime since other proxies may map to valid security classes
                    
                # Make sure security class in the request is allowed (in AllowedVOs)
                if frontend_whitelist == "On" and security_list.has_key(client_security_name):
                    if x509_proxy_security_class in security_list[client_security_name] or "All" in security_list[client_security_name]:
                        in_downtime = prev_downtime
                        logSupport.log.debug("Security test passed for : %s %s " % (jobDescript.data['EntryName'], x509_proxy_security_class))
                    else:
                        logSupport.log.warning("Security class not in whitelist, skipping (%s %s) " % (client_security_name, x509_proxy_security_class))
                        continue
                else:
                    pass
                    # already checked security name 
                             
                # Check that security class maps to a username for submission                   
                x509_proxy_username = x509_proxies.get_username(x509_proxy_security_class)
                if x509_proxy_username == None:
                    logSupport.log.warning("No mapping for security class %s of x509_proxy_%i for %s (secid: %s), skipping and trying the others" % (x509_proxy_security_class, i, client_int_name, client_security_name))
                    continue 
    
                # Format proxy filename
                try:
                    x509_proxy_fname = glideFactoryLib.update_x509_proxy_file(entry_name, x509_proxy_username, "%s_%s" % (work_key, x509_proxy_identifier), x509_proxy)
                except RuntimeError,e:
                    logSupport.log.warning("Failed to update x509_proxy_%i using username %s for client %s, skipping request" % (i, x509_proxy_username, client_int_name))
                    logSupport.log.debug("Failed to update x509_proxy_%i using username %s for client %s: %s" % (i, x509_proxy_username, client_int_name, e))
                    continue 
                except:
                    tb = traceback.format_exception(sys.exc_info()[0], sys.exc_info()[1], sys.exc_info()[2])
                    logSupport.log.warning("Failed to update x509_proxy_%i using usename %s for client %s, skipping request" % (i, x509_proxy_username, client_int_name))
                    logSupport.log.debug("Failed to update x509_proxy_%i using usename %s for client %s: Exception %s" % (i, x509_proxy_username, client_int_name, string.join(tb, '')))
                    continue 
                        
                x509_proxies.add_fname(x509_proxy_security_class, x509_proxy_identifier, x509_proxy_fname)
    
            if x509_proxies.count_fnames < 1:
                if security_class_downtime_found:
                    logSupport.log.warning("Found proxies for client %s but the security class was in downtime, setting entry into downtime for advertising" % client_int_name)
                    in_downtime = True
                else:
                    logSupport.log.warning("No good proxies for %s, skipping request" % client_int_name)
                    continue
            else:
                security_credentials['x509_proxy_list'] = x509_proxies
 
            # ========== end v2+ protocol =============
                 
        else:
            # ========== v3+ proxy protocol ===============
            
            # Get credential security class 
            if decrypted_params.has_key('SecurityClass'):
                credential_security_class = decrypted_params['SecurityClass']
            else:
                logSupport.log.warning("Client %s did not provide a security class. Skipping bad request" % client_int_name)
                continue #skip request
                    
            # Check security class for downtime (in downtimes file)
            logSupport.log.info("Checking downtime for frontend %s security class: %s (entry %s)." % (client_security_name, credential_security_class, jobDescript.data['EntryName']))
            in_sec_downtime = (factory_downtimes.checkDowntime(entry="factory", frontend=client_security_name, security_class=credential_security_class) or
                                factory_downtimes.checkDowntime(entry=jobDescript.data['EntryName'], frontend=client_security_name, security_class=credential_security_class))
            if in_sec_downtime:
                logSupport.log.warning("Security Class %s is currently in a downtime window for Entry: %s. Ignoring request." % (credential_security_class, jobDescript.data['EntryName']))
                continue # cannot use proxy for submission but entry is not in downtime since other proxies may map to valid security classes
            
            # Make sure security class in the request is allowed (in AllowedVOs)
            if frontend_whitelist == "On" and security_list.has_key(client_security_name):
                if credential_security_class in security_list[client_security_name] or "All" in security_list[client_security_name]:
                    in_downtime = prev_downtime
                    logSupport.log.debug("Security test passed for : %s %s " % (jobDescript.data['EntryName'], credential_security_class))
                else:
                    logSupport.log.warning("Security class not in whitelist, skipping (%s %s) " % (client_security_name, credential_security_class))
                    continue
            else:
                pass # already checked security name      
              
            # Check that security class maps to a username for submission                   
            credential_username = frontendDescript.get_username(client_security_name, credential_security_class)
            if credential_username == None:
                logSupport.log.warning("No username mapping for security class %s of x509_proxy_%i for %s (secid: %s), skipping and trying the others" % (credential_security_class, i, client_int_name, client_security_name))
                continue 
            
            # Initialize submit credential object
            submit_credentials = SubmitCredentials(credential_username, credential_security_class)
                                                
            # Determine the credential location  
            submit_credentials.cred_dir = os.path.join(client_proxies_base_dir, "user_%s/glidein_%s" % (credential_username, glidein_name))
            
            # Validate authentication/authorization according to auth methods listed 
            # Grid sites do not require VM id or type.  All have proxy in their auth method
            if 'grid_proxy' in auth_method:
                            
                # Get proxy id and make safe to write files
                if decrypted_params.has_key('x509SubmitProxy'):
                    
                    # Determine identifier for file name and add to credentials to be passed to submit
                    proxy_id = decrypted_params['x509SubmitProxy']
                    if not submit_credentials.add_security_credential('SubmitProxy', proxy_id):
                        logSupport.log.warning("Credential %s for the submit proxy cannot be found for client %s, skipping request" % (proxy_id, client_int_name))
                        continue #skip proxy
                    
                    # Set the id used for tracking what is in the factory queue       
                    submit_credentials.id = proxy_id
                                                                      
                    # Check if voms_attr is required
                    if 'voms_attr' in auth_method:
                        # TODO determine how to verify voms attribute on a proxy
                        pass   
                                
                else:
                    logSupport.log.warning("Client %s did not provide the required submit proxy, skipping request" % client_int_name)
                    continue #skip proxy
                
            else:
                # All non proxy auth methods are cloud sites. 
                
                # Verify that the glidein proxy was provided for the non-proxy auth methods
                if decrypted_params.has_key('GlideinProxy'):
                    proxy_id = decrypted_params['GlideinProxy']
                    if not submit_credentials.add_security_credential('GlideinProxy', proxy_id):
                        logSupport.log.warning("Credential %s for the glidein proxy cannot be found for client %s, skipping request" % (proxy_id, client_int_name))
                        continue  
                else:  
                    logSupport.log.warning("Glidein proxy cannot be found for client %s, skipping request" % client_int_name)
                
                # VM id and type are required for cloud sites
                if 'vm_id' in auth_method:                  
                    # Otherwise the Frontend should supply it
                    if decrypted_params.has_key('VMId'):   
                        submit_credentials.add_identity_credential('VMId', decrypted_params['VMId'])
                    else:
                        logSupport.log.info("Client '%s' did not specify a VM Id in the request, this is required by entry %s, skipping "%(client_int_name, jobDescript.data['EntryName']))
                        continue  
                else:
                    # Validate factory provided vm id exists
                    if jobDescript.has_key('FactoryVMId'): 
                        submit_credentials.add_identity_credential('VMId', jobDescript['FactoryVMId'])
                    else:
                        logSupport.log.info("Entry does not specify a VM Id, this is required by entry %s, skipping "% jobDescript.data['EntryName'])
                        continue  
                    
                if 'vm_type' in auth_method:                  
                    # Otherwise the Frontend should supply it
                    if decrypted_params.has_key('VMType'):
                        submit_credentials.add_identity_credential('VMType', decrypted_params['VMType'])
                    else:
                        logSupport.log.info("Client '%s' did not specify a VM Type in the request, this is required by entry %s, skipping "%(client_int_name, jobDescript.data['EntryName']))
                        continue
                else:
                    # Validate factory provided vm type exists
                    if jobDescript.has_key('FactoryVMType'): 
                        submit_credentials.add_identity_credential('VMType', jobDescript['FactoryVMType'])
                    else:
                        logSupport.log.info("Entry does not specify a VM Type, this is required by entry %s, skipping "% jobDescript.data['EntryName'])
                        continue
                
                if 'x509_cert_pair' in auth_method :
                    # Validate both the public and private certs were passed
                    if decrypted_params.has_key('Publicx509Cert') and decrypted_params.has_key('Privatex509Cert'):
                        
                        public_cert_id = decrypted_params['Publicx509Cert']
                        if not submit_credentials.add_security_credential('Publicx509Cert', public_cert_id):
                            logSupport.log.warning("Credential %s for the public x509 certificate is not safe for client %s, skipping request" % (public_cert_id, client_int_name))
                            continue #skip   
                        
                        private_cert_id = decrypted_params['Privatex509Cert']
                        if not submit_credentials.add_security_credential('Privatex509Cert', private_cert_id):
                            logSupport.log.warning("Credential %s for the private x509 certificate is not safe for client %s, skipping request" % (private_cert_id, client_int_name))
                            continue #skip   
                        
                    else:
                        # project id is required, cannot service request
                        logSupport.log.warning("Client '%s' did not specify the x509 certificate pair in the request, this is required by entry %s, skipping "%(client_int_name, jobDescript.data['EntryName']))
                        continue
                        
                elif 'key_pair' in auth_method:
                    # Validate both the public and private keys were passed
                    if decrypted_params.has_key('PublicKey') and decrypted_params.has_key('PrivateKey'):
                        
                        public_key_id = decrypted_params['PublicKey']
                        if not submit_credentials.add_security_credential('PublicKey', public_key_id):
                            logSupport.log.warning("Credential %s for the public key is not safe for client %s, skipping request" % (public_key_id, client_int_name))
                            continue #skip   
                        
                        private_key_id = decrypted_params['PrivateKey']
                        if not submit_credentials.add_security_credential('PrivateKey', private_key_id):
                            logSupport.log.warning("Credential %s for the private key is not safe for client %s, skipping request" % (private_key_id, client_int_name))
                            continue #skip  
                    else:
                        # project id is required, cannot service request
                        logSupport.log.warning("Client '%s' did not specify the key pair in the request, this is required by entry %s, skipping "%(client_int_name, jobDescript.data['EntryName']))
                        continue
                    
                elif 'username_password' in auth_method:
                    # Validate both the public and private keys were passed
                    if decrypted_params.has_key('Username') and decrypted_params.has_key('Password'):                        
                        username_id = decrypted_params['Username']
                        if not submit_credentials.add_security_credential('Username', username_id):
                            logSupport.log.warning("Credential %s for the username is not safe for client %s, skipping request" % (username_id, client_int_name))
                            continue    
                        
                        password_id = decrypted_params['Password']
                        if not submit_credentials.add_security_credential('Password', password_id):
                            logSupport.log.warning("Credential %s for the password is not safe for client %s, skipping request" % (password_id, client_int_name))
                            continue    
                        
                    else:
                        # project id is required, cannot service request
                        logSupport.log.warning("Client '%s' did not specify the username and password in the request, this is required by entry %s, skipping "%(client_int_name, jobDescript.data['EntryName']))
                        continue
                    
                else:
                    # only method left allowed is factory
                    if not ('factory' in auth_method):
                        logSupport.log.warning("Factory has invalid authentication method. Skipping bad request.")
                        continue #skip request
                    
                    # Check no crendentials were passed in the request 
                    if decrypted_params.has_key('x509SubmitProxy') or decrypted_params.has_key('GlideinProxy') or \
                            decrypted_params.has_key('Publicx509Cert') or decrypted_params.has_key('Privatex509Cert') or \
                            decrypted_params.has_key('PublicKey') and decrypted_params.has_key('PrivateKey') or \
                            decrypted_params.has_key('Username') or decrypted_params.has_key('Password'):
                        logSupport.log.warning("Client %s provided credentials but only factory credentials are allowed. Skipping bad request" % client_int_name)
                        continue #skip request
                    
                    # only support factory supplying one credential set (list of proxies not supported)
                    credential_security_class = "factory"
                    credential_username = frontendDescript[client_security_name]['usermap'][credential_security_class]
                    
                    if credential_username == None:
                        logSupport.log.warning("No mapping for security class %s for %s (secid: %s), skipping " % (credential_security_class, client_int_name, client_security_name))
                        continue # cannot map, frontend
                    
                    # Check factory has needed credentials
                    if auth_method == 'factory_grid_proxy':     
            
                        if glideinDescript.has_key('FactoryProxy'):
                            # KEL TODO - should the installer put the proxy into the correct location and we just verify it exists?
                            # if glideinDescript has the filepath, then how does keepIdleGlideins and submitGlideins know the difference?
                            proxy_id = glideinDescript['FactoryProxy']                         
                            if not submit_credentials.add_security_credential('SubmitProxy', proxy_id):
                                logSupport.log.warning("Could not find factory proxy %s for client %s, skipping request" % (proxy_id, client_int_name))
                                continue   
                             
                        else: 
                            # project id is required, cannot service request
                            logSupport.log.warning("The Factory did not specify the submit proxy in the config, this is required by entry %s, skipping " %jobDescript.data['EntryName'])
                            continue        
                          
                        # Check if voms_attr is required
                        if 'voms_attr' in auth_method:
                            # TODO determine how to verify voms attribute on a proxy
                            pass                    
                    
                    elif auth_method == 'factory_x509_cert_pair':
                        # Validate that the public/private cert pair was configured
                        if glideinDescript.has_key('FactoryPublicx509Cert') and glideinDescript.has_key('FactoryPrivatex509Cert'):
                            public_cert_id = glideinDescript['FactoryPublicx509Cert']
                            if not submit_credentials.add_security_credential('Publicx509Cert', public_cert_id):
                                logSupport.log.warning("Credential %s for the Factory provided public x509 certificate is not safe for client %s, skipping request" % (public_cert_id, client_int_name))
                                continue    
                            
                            private_cert_id = glideinDescript['FactoryPrivatex509Cert']
                            if not submit_credentials.add_security_credential('Privatex509Cert', private_cert_id):
                                logSupport.log.warning("Credential %s for the Factory provided private x509 certificate is not safe for client %s, skipping request" % (private_cert_id, client_int_name))
                                continue    
                        else:
                            # project id is required, cannot service request
                            logSupport.log.warning("The Factory did not specify the x509 certificate pair in the config, this is required by entry %s, skipping " %jobDescript.data['EntryName'])
                            continue
                            
                    elif auth_method == 'factory_key_pair':
                        # Validate that the public/private key pair was configured
                        if jobDescript.has_key('FactoryPublicKey') and jobDescript.has_key('FactoryPrivateKey'):
                            public_key_id = jobDescript['FactoryPublicKey']
                            if not submit_credentials.add_security_credential('PublicKey', public_key_id):
                                logSupport.log.warning("Credential %s for the Factory provided public key is not safe for client %s, skipping request" % (public_key_id, client_int_name))
                                continue    
                            
                            private_key_id = jobDescript['FactoryPrivateKey']
                            if not submit_credentials.add_security_credential('PrivateKey', private_key_id):
                                logSupport.log.warning("Credential %s for the Factory provided private key is not safe for client %s, skipping request" % (private_key_id, client_int_name))
                                continue   
                        else:
                            # project id is required, cannot service request
                            logSupport.log.warning("The Factory did not specify the key pair in the config, this is required by entry %s, skipping " % jobDescript.data['EntryName'])
                            continue
                            
                    elif auth_method == 'factory_username_password':
                        # Validate that the username and password were configured
                        if jobDescript.has_key('FactoryUsername') and jobDescript.has_key('FactoryPassword'):
                            username_id = jobDescript['FactoryUsername']
                            if not submit_credentials.add_security_credential('Username', username_id):
                                logSupport.log.warning("Credential %s for the Factory provided username is not safe for client %s, skipping request" % (username_id, client_int_name))
                                continue    
                        
                            password_id = jobDescript['FactoryPassword']
                            if not submit_credentials.add_security_credential('Password', password_id):
                                logSupport.log.warning("Credential %s for the Factory provided password is not safe for client %s, skipping request" % (password_id, client_int_name))
                                continue    
                        else:
                            # project id is required, cannot service request
                            logSupport.log.warning("The Factory did not specify the username and password in the config, this is required by entry %s, skipping " %jobDescript.data['EntryName'])
                            continue
                        
                    else:
                        pass
            
            # ========== end of v3+ proxy protocol ===============
            ##### END - CREDENTIAL HANDLING - END #####

        jobAttributes.data['GLIDEIN_In_Downtime'] = in_downtime
        glideFactoryLib.factoryConfig.qc_stats.set_downtime(in_downtime) #@UndefinedVariable

        if work[work_key]['requests'].has_key('RemoveExcess'):
            remove_excess = work[work_key]['requests']['RemoveExcess']
        else:
            remove_excess = 'NO'

        if work[work_key]['requests'].has_key('IdleGlideins'):
            # malformed, if no IdleGlideins
            try:
                idle_glideins = int(work[work_key]['requests']['IdleGlideins'])
            except ValueError, e:
                logSupport.log.warning("Client %s provided an invalid ReqIdleGlideins: '%s' not a number. Skipping request" % (client_int_name, work[work_key]['requests']['IdleGlideins']))
                continue #skip request

            if idle_glideins > factory_max_idle:
                idle_glideins = factory_max_idle

            if work[work_key]['requests'].has_key('MaxRunningGlideins'):
                try:
                    max_running = int(work[work_key]['requests']['MaxRunningGlideins'])
                except ValueError, e:
                    logSupport.log.warning("Client %s provided an invalid ReqMaxRunningGlideins: '%s' not a number. Skipping request" % (client_int_name, work[work_key]['requests']['MaxRunningGlideins']))
                    continue #skip request
                if max_running > factory_max_running:
                    max_running = factory_max_running
            else:
                max_running = factory_max_running

            if in_downtime:
                # we are in downtime... no new submissions
                idle_glideins = 0

            if work[work_key]['web'].has_key('URL'):
                try:
                    client_web_url = work[work_key]['web']['URL']
                    client_signtype = work[work_key]['web']['SignType']
                    client_descript = work[work_key]['web']['DescriptFile']
                    client_sign = work[work_key]['web']['DescriptSign']

                    if work[work_key]['internals'].has_key('GroupName'):
                        client_group = work[work_key]['internals']['GroupName']
                        client_group_web_url = work[work_key]['web']['GroupURL']
                        client_group_descript = work[work_key]['web']['GroupDescriptFile']
                        client_group_sign = work[work_key]['web']['GroupDescriptSign']
                        client_web = glideFactoryLib.ClientWeb(client_web_url, client_signtype, client_descript, client_sign,
                                                    client_group, client_group_web_url, client_group_descript, client_group_sign)
                    else:
                        # new style, but without a group (basic frontend)
                        client_web = glideFactoryLib.ClientWebNoGroup(client_web_url, client_signtype, client_descript, client_sign)
                except:
                    # malformed classad, skip
                    logSupport.log.warning("Malformed classad for client %s, skipping" % work_key)
                    continue
            else:
                # old style
                client_web = None
            
            if security_credentials.has_key('x509_proxy_list'):
                # ======= v2+ support for multiple proxies ==========
                x509_proxies = security_credentials['x509_proxy_list']
                x509_proxy_security_classes = x509_proxies.fnames.keys()
                x509_proxy_security_classes.sort() # sort to have consistent logging
                for x509_proxy_security_class in x509_proxy_security_classes:
                    # submit each security class independently
                    # split the request proportionally between them
                    x509_proxy_frac = 1.0 * len(x509_proxies.fnames[x509_proxy_security_class]) / x509_proxies.count_fnames
    
                    # round up... if a client requests a non splittable number, worse for him
                    # expect to not be a problem in real world as
                    # the most reasonable use case has a single proxy_class per client name
                    idle_glideins_pc = int(math.ceil(idle_glideins * x509_proxy_frac))
                    max_running_pc = int(math.ceil(max_running * x509_proxy_frac))
    
                    # Should log here or in perform_work
                    glideFactoryLib.logWorkRequest(client_int_name, client_security_name, x509_proxy_security_class,
                                                   idle_glideins, max_running, work[work_key], x509_proxy_frac)
    
                    all_security_names.add((client_security_name, x509_proxy_security_class))
                    entry_condorQ = glideFactoryLib.getQProxSecClass(condorQ, client_int_name, x509_proxy_security_class)

                    done_something += perform_work(entry_name, entry_condorQ,
                                                   work_key, client_int_name, client_security_name,
                                                   x509_proxy_security_class, client_int_req,
                                                   in_downtime, remove_excess,
                                                   idle_glideins_pc, max_running_pc, factory_max_held,
                                                   jobDescript, x509_proxies.fnames[x509_proxy_security_class], x509_proxies.get_username(x509_proxy_security_class),
                                                   identity_credentials,
                                                   client_web, params)            
                # ======= end of v2+ support for multiple proxies ==========
                
            else:
                # do one iteration for the credential set (maps to a single security class)
                glideFactoryLib.factoryConfig.client_internals[client_int_name] = {"CompleteName":client_int_name, "ReqName":client_int_req}
                condorStatus = None       
            
                # find out the users it is using
                log_stats = {}
                log_stats[credential_username] = glideFactoryLogParser.dirSummaryTimingsOut(glideFactoryLib.factoryConfig.get_client_log_dir(entry_name, credential_username),
                                                                                          logSupport.log_dir, client_int_name, credential_username)
                # should not need privsep for reading logs
                log_stats[credential_username].load()  
            
                glideFactoryLib.logStats(condorQ, condorStatus, client_int_name, client_security_name, credential_security_class)   
                client_log_name = glideFactoryLib.secClass2Name(client_security_name, credential_security_class)   
                glideFactoryLib.factoryConfig.log_stats.logSummary(client_log_name, log_stats) #@UndefinedVariable  
            
                remove_excess_wait = False
                remove_excess_idle = False
                remove_excess_run = False
                if remove_excess == 'NO':
                    pass # use defaults
                elif remove_excess == 'WAIT':
                    remove_excess_wait = True
                elif remove_excess == 'IDLE':
                    remove_excess_wait = True
                    remove_excess_idle = True
                elif remove_excess == 'ALL':
                    remove_excess_wait = True
                    remove_excess_idle = True
                    remove_excess_run = True
                else:
                    # nothing to do
                    logSupport.log.info("Unknown RemoveExcess '%s', assuming 'NO'" % remove_excess)
                
                nr_submitted = glideFactoryLib.keepIdleGlideins(condorQ, client_int_name, in_downtime,
                                                             remove_excess_wait, remove_excess_idle, remove_excess_run,
                                                             idle_glideins, max_running, factory_max_held,
                                                             submit_credentials, 
                                                             client_web, params)

    logSupport.log.debug("Updating statistics")
    for sec_el in all_security_names:
        try:
            glideFactoryLib.factoryConfig.rrd_stats.getData("%s_%s" % sec_el) #@UndefinedVariable
        except glideFactoryLib.condorExe.ExeError, e:
            # never fail for monitoring... just log
            logSupport.log.warning("get_RRD_data failed: %s" % e)
        except:
            # never fail for monitoring... just log
            logSupport.log.warning("get_RRD_data failed: error unknown")


    return done_something

############################################################
def write_stats():
    global log_rrd_thread
    global qc_rrd_thread

    glideFactoryLib.factoryConfig.log_stats.computeDiff() #@UndefinedVariable
    glideFactoryLib.factoryConfig.log_stats.write_file() #@UndefinedVariable
    logSupport.log.info("log_stats written")

    glideFactoryLib.factoryConfig.qc_stats.finalizeClientMonitor() #@UndefinedVariable
    glideFactoryLib.factoryConfig.qc_stats.write_file() #@UndefinedVariable
    logSupport.log.info("qc_stats written")

    glideFactoryLib.factoryConfig.rrd_stats.writeFiles() #@UndefinedVariable
    logSupport.log.info("rrd_stats written")

    return

# added by C.W. Murphy for glideFactoryEntryDescript
def write_descript(entry_name, entryDescript, entryAttributes, entryParams, monitor_dir):
    entry_data = {entry_name:{}}
    entry_data[entry_name]['descript'] = copy.deepcopy(entryDescript.data)
    entry_data[entry_name]['attributes'] = copy.deepcopy(entryAttributes.data)
    entry_data[entry_name]['params'] = copy.deepcopy(entryParams.data)

    descript2XML = glideFactoryMonitoring.Descript2XML()
    str = descript2XML.entryDescript(entry_data)
    xml_str = ""
    for line in str.split("\n")[1:-2]:
        line = line[3:] + "\n" # remove the extra tab
        xml_str += line

    try:
        descript2XML.writeFile(monitor_dir + "/", xml_str, singleEntry=True)
    except IOError:
        logSupport.log.debug("IOError in writeFile in descript2XML")

    return


############################################################
def advertize_myself(in_downtime, glideinDescript, jobDescript, jobAttributes, jobParams):
    entry_name = jobDescript.data['EntryName']
    trust_domain = jobDescript.data['TrustDomain']
    auth_method = jobDescript.data['AuthMethod']
    pub_key_obj = glideinDescript.data['PubKeyObj']

    glideFactoryLib.factoryConfig.client_stats.finalizeClientMonitor() #@UndefinedVariable

    current_qc_total = glideFactoryLib.factoryConfig.client_stats.get_total() #@UndefinedVariable

    glidein_monitors = {}
    for w in current_qc_total.keys():
        for a in current_qc_total[w].keys():
            glidein_monitors['Total%s%s' % (w, a)] = current_qc_total[w][a]
    try:
        myJobAttributes = jobAttributes.data.copy()
        myJobAttributes['GLIDEIN_In_Downtime'] = in_downtime
        glideFactoryInterface.advertizeGlidein(glideFactoryLib.factoryConfig.factory_name,
                                               glideFactoryLib.factoryConfig.glidein_name,
                                               entry_name,
                                               trust_domain,
                                               auth_method,
                                               glideFactoryLib.factoryConfig.supported_signtypes,
                                               pub_key_obj,
                                               myJobAttributes,
                                               jobParams.data.copy(),
                                               glidein_monitors.copy())
    except:
        logSupport.log.warning("Advertize failed")

    # Advertise the monitoring
    advertizer = glideFactoryInterface.MultiAdvertizeGlideinClientMonitoring(glideFactoryLib.factoryConfig.factory_name,
                                                                             glideFactoryLib.factoryConfig.glidein_name,
                                                                             entry_name,
                                                                             jobAttributes.data.copy())

    current_qc_data = glideFactoryLib.factoryConfig.client_stats.get_data() #@UndefinedVariable
    for client_name in current_qc_data.keys():
        client_qc_data = current_qc_data[client_name]
        if not glideFactoryLib.factoryConfig.client_internals.has_key(client_name): #@UndefinedVariable
            logSupport.log.warning("Client '%s' has stats, but no classad! Ignoring." % client_name)
            continue

        client_internals = glideFactoryLib.factoryConfig.client_internals[client_name]

        client_monitors = {}
        for w in client_qc_data.keys():
            for a in client_qc_data[w].keys():
                if type(client_qc_data[w][a]) == type(1): # report only numbers
                    client_monitors['%s%s' % (w, a)] = client_qc_data[w][a]

        try:
            fparams = current_qc_data[client_name]['Requested']['Parameters']
        except:
            fparams = {}

        params = jobParams.data.copy()
        for p in fparams.keys():
            if p in params.keys(): # can only overwrite existing params, not create new ones
                params[p] = fparams[p]

        advertizer.add(client_internals["CompleteName"], client_name, client_internals["ReqName"], params, client_monitors.copy())

    try:
        advertizer.do_advertize()
    except:
        logSupport.log.warning("Advertize of monitoring failed")

    return

############################################################
def iterate_one(do_advertize, in_downtime, glideinDescript, frontendDescript, jobDescript, jobAttributes, jobParams):
    done_something = 0
    # Set the downtime flag
    jobAttributes.data['GLIDEIN_In_Downtime'] = in_downtime

    try:
        done_something = find_and_perform_work(in_downtime, glideinDescript, frontendDescript, jobDescript, jobAttributes, jobParams)
    except:
        logSupport.log.warning("Error occurred while trying to find and do work.")

    if do_advertize or done_something:
        logSupport.log.info("Advertize")
        advertize_myself(in_downtime, glideinDescript, jobDescript, jobAttributes, jobParams)

    # Why do we delete this attribute?  Seems rather unecessary
    del jobAttributes.data['GLIDEIN_In_Downtime']

    return done_something

############################################################
def iterate(parent_pid, sleep_time, advertize_rate, glideinDescript, frontendDescript, jobDescript, jobAttributes, jobParams):
    """iterate function

    The main "worker" function for the Factory Entry.
    @todo: More description to come

    @type parent_pid: int
    @param parent_pid: the pid for the Factory daemon
    @type sleep_time: int
    @param sleep_time: The number of seconds to sleep between iterations
    @type advertise_rate: int
    @param advertise_rate: The rate at which advertising should occur (CHANGE ME... THIS IS NOT HELPFUL)
    @type glideinDescript: glideFactoryConfig.GlideinDescript
    @param glideinDescript: Object that encapsulates glidein.descript in the Factory root directory
    @type frontendDescript: glideFactoryConfig.FrontendDescript
    @param frontendDescript: Object that encapsulates frontend.descript in the Factory root directory
    @type jobDescript: glideFactoryConfig.JobDescript
    @param jobDescript: Object that encapsulates job.descript in the entry directory
    @type jobAttributes: glideFactoryConfig.JobAttributes
    @param jobAttributes: Object that encapsulates attributes.cfg in the entry directory
    @type jobParams: glideFactoryConfig.JobParams
    @param jobParams: Object that encapsulates params.cfg in the entry directory


    """
    is_first = 1
    count = 0

    # Set Monitoring logging
    glideFactoryLib.factoryConfig.log_stats = glideFactoryMonitoring.condorLogSummary()
    glideFactoryLib.factoryConfig.rrd_stats = glideFactoryMonitoring.FactoryStatusData()

    # Get downtimes
    factory_downtimes = glideFactoryDowntimeLib.DowntimeFile(glideinDescript.data['DowntimesFile'])

    # Start work loop
    while 1:
        # check to see if we are orphaned - raises KeyboardInterrupt exception if we are
        check_parent(parent_pid, glideinDescript, jobDescript)

        # are we in downtime?  True/False
        in_downtime = (factory_downtimes.checkDowntime(entry="factory") or
                       factory_downtimes.checkDowntime(entry=jobDescript.data['EntryName']))
        if in_downtime:
            logSupport.log.info("Downtime iteration at %s" % time.ctime())
        else:
            logSupport.log.info("Iteration at %s" % time.ctime())

        try:
            glideFactoryLib.factoryConfig.log_stats.reset() #@UndefinedVariable
            # This one is used for stats advertized in the ClassAd
            glideFactoryLib.factoryConfig.client_stats = glideFactoryMonitoring.condorQStats()
            # These two are used to write the history to disk
            glideFactoryLib.factoryConfig.qc_stats = glideFactoryMonitoring.condorQStats()
            glideFactoryLib.factoryConfig.client_internals = {}

            # actually do some work now that we have everything setup (hopefully)
            done_something = iterate_one(count == 0, in_downtime, glideinDescript, frontendDescript,
                                         jobDescript, jobAttributes, jobParams)

            logSupport.log.info("Writing stats")
            try:
                write_stats()
            except KeyboardInterrupt:
                raise # this is an exit signal, pass through
            except:
                # never fail for stats reasons!
                tb = traceback.format_exception(sys.exc_info()[0], sys.exc_info()[1], sys.exc_info()[2])
                logSupport.log.warning("Exception occurred: %s" % tb)
        except KeyboardInterrupt:
            raise # this is an exit signal, pass through
        except:
            if is_first:
                raise
            else:
                # if not the first pass, just warn
                tb = traceback.format_exception(sys.exc_info()[0], sys.exc_info()[1], sys.exc_info()[2])
                logSupport.log.warning("Exception occurred: %s" % tb)

        cleanupSupport.cleaners.cleanup()

        # Sleep now before next iteration
        logSupport.log.info("Sleep %is" % sleep_time)
        time.sleep(sleep_time)

        count = (count + 1) % advertize_rate
        is_first = 0


############################################################
def main(parent_pid, sleep_time, advertize_rate, startup_dir, entry_name):
    """GlideinFactoryEntry main function

    Setup logging, monitoring, and configuration information.  Starts the Entry
    main loop and handles cleanup at shutdown.

    @type parent_pid: int
    @param parent_pid: The pid for the Factory daemon
    @type sleep_time: int
    @param sleep_time: The number of seconds to sleep between iterations
    @type advertise_rate: int
    @param advertise_rate: The rate at which advertising should occur (CHANGE ME... THIS IS NOT HELPFUL)
    @type startup_dir: string
    @param startup_dir: The "home" directory for the entry.
    @type entry_name: string
    @param entry_name: The name of the entry as specified in the config file
    """
    startup_time = time.time()
    os.chdir(startup_dir)

    glideinDescript = glideFactoryConfig.GlideinDescript()
    glideinDescript.load_pub_key()

    # Check whether the entry name passed in matches a supported entry
    if not (entry_name in string.split(glideinDescript.data['Entries'], ',')):
        raise RuntimeError, "Entry '%s' not supported: %s" % \
            (entry_name, glideinDescript.data['Entries'])

    # Set the Log directory
    logSupport.log_dir = os.path.join(glideinDescript.data['LogDir'], "entry_%s" % entry_name)

    # Configure the process to use the proper LogDir as soon as you get the info
    logSupport.add_glideinlog_handler(entry_name, logSupport.log_dir,
                                      int(float(glideinDescript.data['LogRetentionMaxDays'])),
                                      int(float(glideinDescript.data['LogRetentionMaxMBs'])))
    logSupport.log = logging.getLogger(entry_name)
    logSupport.log.debug("Logging initialized")

    ## Not touching the monitoring logging.  Don't know how that works yet
    logSupport.log.debug("Setting up the monitoring")
    glideFactoryMonitoring.monitoringConfig.monitor_dir = os.path.join(startup_dir, "monitor/entry_%s" % entry_name)
    logSupport.log.debug("Monitoring directory: %s" % glideFactoryMonitoring.monitoringConfig.monitor_dir)
    glideFactoryMonitoring.monitoringConfig.config_log(logSupport.log_dir,
                                                       float(glideinDescript.data['SummaryLogRetentionMaxDays']),
                                                       float(glideinDescript.data['SummaryLogRetentionMinDays']),
                                                       float(glideinDescript.data['SummaryLogRetentionMaxMBs']))

    glideFactoryMonitoring.monitoringConfig.my_name = "%s@%s" % (entry_name, glideinDescript.data['GlideinName'])
    logSupport.log.debug("Monitoring Name: %s" % glideFactoryMonitoring.monitoringConfig.my_name)


    logSupport.log.debug("Getting configurations")
    frontendDescript = glideFactoryConfig.FrontendDescript()
    jobDescript = glideFactoryConfig.JobDescript(entry_name)
    jobAttributes = glideFactoryConfig.JobAttributes(entry_name)
    jobParams = glideFactoryConfig.JobParams(entry_name)

    logSupport.log.debug("Write descripts")
    write_descript(entry_name, jobDescript, jobAttributes, jobParams, glideFactoryMonitoring.monitoringConfig.monitor_dir)

    # use config values to configure the factory
    logSupport.log.debug("Setting Factory (Entry) Config")
    glideFactoryLib.factoryConfig.config_whoamI(glideinDescript.data['FactoryName'], glideinDescript.data['GlideinName'])
    glideFactoryLib.factoryConfig.config_dirs(startup_dir,
                                              glideinDescript.data['LogDir'],
                                              glideinDescript.data['ClientLogBaseDir'],
                                              glideinDescript.data['ClientProxiesBaseDir'])

    glideFactoryLib.factoryConfig.max_submits = int(jobDescript.data['MaxSubmitRate'])
    glideFactoryLib.factoryConfig.max_cluster_size = int(jobDescript.data['SubmitCluster'])
    glideFactoryLib.factoryConfig.submit_sleep = float(jobDescript.data['SubmitSleep'])
    glideFactoryLib.factoryConfig.max_removes = int(jobDescript.data['MaxRemoveRate'])
    glideFactoryLib.factoryConfig.remove_sleep = float(jobDescript.data['RemoveSleep'])
    glideFactoryLib.factoryConfig.max_releases = int(jobDescript.data['MaxReleaseRate'])
    glideFactoryLib.factoryConfig.release_sleep = float(jobDescript.data['ReleaseSleep'])

    logSupport.log.debug("Adding directory cleaners")
    cleaner = cleanupSupport.PrivsepDirCleanupWSpace(None, logSupport.log_dir,
                                      "(condor_activity_.*\.log\..*\.ftstpk)",
                                      int(float(glideinDescript.data['CondorLogRetentionMaxDays'])) * 24 * 3600,
                                      int(float(glideinDescript.data['CondorLogRetentionMinDays'])) * 24 * 3600,
                                      long(float(glideinDescript.data['CondorLogRetentionMaxMBs'])) * (1024.0 * 1024.0))
    cleanupSupport.cleaners.add_cleaner(cleaner)

    # add cleaners for the user log directories
    for username in frontendDescript.get_all_usernames():
        user_log_dir = glideFactoryLib.factoryConfig.get_client_log_dir(entry_name, username)
        cleaner = cleanupSupport.PrivsepDirCleanupWSpace(username, user_log_dir,
                                          "(job\..*\.out)|(job\..*\.err)",
                                          int(float(glideinDescript.data['JobLogRetentionMaxDays'])) * 24 * 3600,
                                          int(float(glideinDescript.data['JobLogRetentionMinDays'])) * 24 * 3600,
                                          long(float(glideinDescript.data['JobLogRetentionMaxMBs'])) * (1024.0 * 1024.0))
        cleanupSupport.cleaners.add_cleaner(cleaner)
        cleaner = cleanupSupport.PrivsepDirCleanupWSpace(username, user_log_dir,
                                          "(condor_activity_.*\.log)|(condor_activity_.*\.log.ftstpk)|(submit_.*\.log)",
                                          int(float(glideinDescript.data['CondorLogRetentionMaxDays'])) * 24 * 3600,
                                          int(float(glideinDescript.data['CondorLogRetentionMinDays'])) * 24 * 3600,
                                          long(float(glideinDescript.data['CondorLogRetentionMaxMBs'])) * (1024.0 * 1024.0))
        cleanupSupport.cleaners.add_cleaner(cleaner)

    logSupport.log.debug("Set advertiser parameters")
    glideFactoryInterface.factoryConfig.advertise_use_tcp = (glideinDescript.data['AdvertiseWithTCP'] in ('True', '1'))
    glideFactoryInterface.factoryConfig.advertise_use_multi = (glideinDescript.data['AdvertiseWithMultiple'] in ('True', '1'))

    logSupport.log.debug("Get glideinWMS version")
    try:
        dir = os.path.dirname(os.path.dirname(sys.argv[0]))
        glideFactoryInterface.factoryConfig.glideinwms_version = glideinWMSVersion.GlideinWMSDistro(dir, os.path.join(dir, 'etc/checksum.factory')).version()
    except:
        tb = traceback.format_exception(sys.exc_info()[0], sys.exc_info()[1], sys.exc_info()[2])
        logSupport.log.warning("Exception occured while trying to retrieve the glideinwms version. See debug log for more details.")
        logSupport.log.debug("Exception occurred: %s" % tb)


    # create lock file
    pid_obj = glideFactoryPidLib.EntryPidSupport(startup_dir, entry_name)
    logSupport.log.debug("Created lock file")

    # force integrity checks on all the operations
    # I need integrity checks also on reads, as I depend on them
    os.environ['_CONDOR_SEC_DEFAULT_INTEGRITY'] = 'REQUIRED'
    os.environ['_CONDOR_SEC_CLIENT_INTEGRITY'] = 'REQUIRED'
    os.environ['_CONDOR_SEC_READ_INTEGRITY'] = 'REQUIRED'
    os.environ['_CONDOR_SEC_WRITE_INTEGRITY'] = 'REQUIRED'
    logSupport.log.debug("Set Condor security environment")

    # If authentication method is factory, verify that the environ is set
    if 'factory' in jobDescript.data['AuthMethod']:
        if not os.environ.has_key('X509_USER_PROXY'):
            logSupport.log.warning("Factory is supposed to provide a proxy for this entry, but environment variable X509_USER_PROXY not set. Need X509_USER_PROXY to work!")
            # KEL TODO - raise error or just log warning????
            raise RuntimeError, "Factory is supposed to provide a proxy for this entry. Need X509_USER_PROXY to work!"

    # start
    pid_obj.register(parent_pid)
    try:
        try:
            try:
                logSupport.log.info("Starting up")
                iterate(parent_pid, sleep_time, advertize_rate,
                        glideinDescript, frontendDescript, jobDescript, jobAttributes, jobParams)
            except KeyboardInterrupt:
                logSupport.log.info("Received signal...exit")
            except:
                tb = traceback.format_exception(sys.exc_info()[0], sys.exc_info()[1], sys.exc_info()[2])
                logSupport.log.warning("Exception occurred: %s" % tb)
                raise
        finally:
            # no need to cleanup.. the parent should be doing it
            logSupport.log.info("Dying")
    finally:
        pid_obj.relinquish()

    
class SubmitCredentials:
    """
    Data class containing all information needed to submit a glidein.
    """
    def __init__(self, username, security_class):
        self.username = username # are we using privsep or not
        self.security_class = security_class # is this needed?  why are we passing this?
        self.id = None # id used for tracking the credentials used for submitting in the schedd
        self.cred_dir = ''  # location of credentials
        self.security_credentials = {} # dict of credentials
        self.identity_credentials = {} # identity informatin passed by frontend
    
    def add_security_credential(self, cred_type, filename):
        """
        Return the full path to the credential.
        """
        if not is_str_safe(filename):
            return False 
        
        cred_fname = os.path.join(self.cred_dir, 'credential_%s' % filename)
        if not os.path.isfile(cred_fname):
            return False 
  
        self.security_credentials[cred_type] = cred_fname
        return True
        
    def add_identity_credential(self, cred_type, cred_str):
        """
        Return the full path to the credential.
        """
        self.identity_credentials[cred_type] = cred_str
        return True
    
    def __repr__(self):
        output = "SubmitCredentials"
        output += "username = ", self.username
        output += "security class = ", self.security_class
        output += "id = ", self.id
        output += "cedential dir = ", self.cred_dir
        output += "security credentials: "
        for sc in self.security_credentials.keys():
            output += "    %s : %s" % (sc, self.security_credentials[sc])
        output += "identity credentials: "
        for ic in self.identity_credentials.keys():
            output += "    %s : %s" % (ic, self.identity_credentials[ic])
        return output

############################################################
#
# S T A R T U P
#
############################################################

def termsignal(signr, frame):
    raise KeyboardInterrupt, "Received signal %s" % signr

if __name__ == '__main__':
    signal.signal(signal.SIGTERM, termsignal)
    signal.signal(signal.SIGQUIT, termsignal)
    main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4], sys.argv[5])


