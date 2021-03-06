#
# Project:
#   glideinWMS
#
# File Version:
#
# Description:
#   This module implements the functions to execute condor commands
#
# Author:
#   Igor Sfiligoi (Sept 7th 2006)
#


import os
import os.path
import popen2
import string
import select
import cStringIO
import fcntl
import time

class UnconfigError(RuntimeError):
    def __init__(self,str):
        RuntimeError.__init__(self,str)

class ExeError(RuntimeError):
    def __init__(self,str):
        RuntimeError.__init__(self,str)

#
# Configuration
#

# Set path to condor binaries, if needed
def set_path(new_condor_bin_path,new_condor_sbin_path=None):
    global condor_bin_path,condor_sbin_path
    condor_bin_path=new_condor_bin_path
    if new_condor_sbin_path!=None:
        condor_sbin_path=new_condor_sbin_path

#
# Execute an arbitrary condor command and return its output as a list of lines
#  condor_exe uses a relative path to $CONDOR_BIN
# Fails if stderr is not empty
#

# can throw UnconfigError or ExeError
def exe_cmd(condor_exe,args,stdin_data=None,env={}):
    global condor_bin_path

    if condor_bin_path==None:
        raise UnconfigError, "condor_bin_path is undefined!"
    condor_exe_path=os.path.join(condor_bin_path,condor_exe)

    cmd="%s %s" % (condor_exe_path,args)

    return iexe_cmd(cmd,stdin_data,env)

def exe_cmd_sbin(condor_exe,args,stdin_data=None,env={}):
    global condor_sbin_path

    if condor_sbin_path==None:
        raise UnconfigError, "condor_sbin_path is undefined!"
    condor_exe_path=os.path.join(condor_sbin_path,condor_exe)

    cmd="%s %s" % (condor_exe_path,args)

    return iexe_cmd(cmd,stdin_data,env)

############################################################
#
# P R I V A T E, do not use
#
############################################################

# can throw ExeError
def iexe_cmd(cmd, stdin_data=None,env={}):
    """Fork a process and execute cmd - rewritten to use select to avoid filling
    up stderr and stdout queues.

    @type cmd: string
    @param cmd: Sting containing the entire command including all arguments
    @type stdin_data: string
    @param stdin_data: Data that will be fed to the command via stdin
    @type env: dict
    @param env: Environment to be set before execution
    """
    output_lines = None
    error_lines = None
    exitStatus = 0
    try:
        saved_env={}
        try:
            # save and set the environment
            for k in env.keys():
                if os.environ.has_key(k):
                    saved_env[k]=os.environ[k]
                    if env[k]==None:
                        del os.environ[k]
                else:
                    saved_env[k]=None
                if env[k]!=None:
                    os.environ[k]=env[k]

            # launch process
            child = popen2.Popen3(cmd, capturestderr=True)
        finally:
            # restore the environemnt
            for k in saved_env.keys():
                if saved_env[k]==None:
                    del os.environ[k]
                else:
                    os.environ[k]=saved_env[k]
            
        if stdin_data != None:
            child.tochild.write(stdin_data)

        child.tochild.close()

        stdout = child.fromchild
        stderr = child.childerr

        outfd = stdout.fileno()
        errfd = stderr.fileno()

        outdata = cStringIO.StringIO()
        errdata = cStringIO.StringIO()

        fdlist = [outfd, errfd]
        for fd in fdlist: # make stdout/stderr nonblocking
            fl = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

        while fdlist:
            time.sleep(.001) # prevent 100% CPU spin
            ready = select.select(fdlist, [], [])
            if outfd in ready[0]:
                outchunk = stdout.read()
                if outchunk == '':
                    fdlist.remove(outfd)
                else:
                    outdata.write(outchunk)

            if errfd in ready[0]:
                errchunk = stderr.read()
                if errchunk == '':
                    fdlist.remove(errfd)
                else:
                    errdata.write(errchunk)

        exitStatus = child.wait()
        outdata.seek(0)
        errdata.seek(0)
        output_lines = outdata.readlines()
        error_lines = errdata.readlines()

    except Exception, ex:
        raise ExeError, "Unexpected Error running '%s'\nStdout:%s\nStderr:%s\n" \
            "Exception OSError: %s" % (cmd, str(output_lines), str(error_lines), ex)

    if exitStatus:
        raise ExeError, "Error running '%s'\ncode %i:%s" % (cmd, os.WEXITSTATUS(exitStatus), "".join(error_lines))

    return output_lines


#
# Set condor_bin_path
#

def init1():
    global condor_bin_path
    # try using condor commands to find it out
    try:
        condor_bin_path=iexe_cmd("condor_config_val BIN")[0][:-1] # remove trailing newline
    except ExeError,e:
        # try to find the RELEASE_DIR, and append bin
        try:
            release_path=iexe_cmd("condor_config_val RELEASE_DIR")
            condor_bin_path=os.path.join(release_path[0][:-1],"bin")
        except ExeError,e:
            # try condor_q in the path
            try:
                condorq_bin_path=iexe_cmd("which condor_q")
                condor_bin_path=os.path.dirname(condorq_bin_path[0][:-1])
            except ExeError,e:
                # look for condor_config in /etc
                if os.environ.has_key("CONDOR_CONFIG"):
                    condor_config=os.environ["CONDOR_CONFIG"]
                else:
                    condor_config="/etc/condor/condor_config"

                try:
                    # BIN = <path>
                    bin_def=iexe_cmd('grep "^ *BIN" %s'%condor_config)
                    condor_bin_path=string.split(bin_def[0][:-1])[2]
                except ExeError, e:
                    try:
                        # RELEASE_DIR = <path>
                        release_def=iexe_cmd('grep "^ *RELEASE_DIR" %s'%condor_config)
                        condor_bin_path=os.path.join(string.split(release_def[0][:-1])[2],"bin")
                    except ExeError, e:
                        pass # don't know what else to try

#
# Set condor_sbin_path
#

def init2():
    global condor_sbin_path
    # try using condor commands to find it out
    try:
        condor_sbin_path=iexe_cmd("condor_config_val SBIN")[0][:-1] # remove trailing newline
    except ExeError,e:
        # try to find the RELEASE_DIR, and append bin
        try:
            release_path=iexe_cmd("condor_config_val RELEASE_DIR")
            condor_sbin_path=os.path.join(release_path[0][:-1],"sbin")
        except ExeError,e:
            # try condor_q in the path
            try:
                condora_sbin_path=iexe_cmd("which condor_advertise")
                condor_sbin_path=os.path.dirname(condora_sbin_path[0][:-1])
            except ExeError,e:
                # look for condor_config in /etc
                if os.environ.has_key("CONDOR_CONFIG"):
                    condor_config=os.environ["CONDOR_CONFIG"]
                else:
                    condor_config="/etc/condor/condor_config"

                try:
                    # BIN = <path>
                    bin_def=iexe_cmd('grep "^ *SBIN" %s'%condor_config)
                    condor_sbin_path=string.split(bin_def[0][:-1])[2]
                except ExeError, e:
                    try:
                        # RELEASE_DIR = <path>
                        release_def=iexe_cmd('grep "^ *RELEASE_DIR" %s'%condor_config)
                        condor_sbin_path=os.path.join(string.split(release_def[0][:-1])[2],"sbin")
                    except ExeError, e:
                        pass # don't know what else to try

def init():
    init1()
    init2()

# This way we know that it is undefined
condor_bin_path=None
condor_sbin_path=None

init()



