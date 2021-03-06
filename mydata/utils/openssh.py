"""
Methods for using OpenSSH functionality from MyData.
On Windows, we bundle a Cygwin build of OpenSSH.

subprocess is used extensively throughout this module.

Given the complex quoting requirements when running remote
commands over ssh, I don't trust Python's
automatic quoting which is done when converting a list of arguments
to a command string in subprocess.Popen.  Furthermore, formatting
the command string ourselves, rather than leaving it to Python
means that we are restricted to using shell=True in subprocess on
POSIX systems.  shell=False seems to work better on Windows,
otherwise we need to worry about escaping special characters like
'>' with carets (i.e. '^>').

"""
import sys
from datetime import datetime
import os
import subprocess
import traceback
import re
import getpass
import threading
import time
import pkgutil
import struct

import psutil

from ..events.stop import ShouldCancelUpload
from ..settings import SETTINGS
from ..logs import logger
from ..models.upload import UploadStatus
from ..utils.exceptions import SshException
from ..utils.exceptions import ScpException
from ..utils.exceptions import PrivateKeyDoesNotExist

from ..subprocesses import DEFAULT_STARTUP_INFO
from ..subprocesses import DEFAULT_CREATION_FLAGS

from .progress import MonitorProgress

if sys.platform.startswith("win"):
    import win32process

if sys.platform.startswith("linux"):
    import mydata.linuxsubprocesses as linuxsubprocesses

# Running subprocess's communicate from multiple threads can cause high CPU
# usage, so we poll each subprocess before running communicate, using a sleep
# interval of SLEEP_FACTOR * maxThreads.
SLEEP_FACTOR = 0.01

CONNECTION_TIMEOUT = 5


class OpenSSH(object):
    """
    A singleton instance of this class (called OPENSSH) is created in this
    module which contains paths to SSH binaries and quoting methods used for
    running remote commands over SSH via subprocesses.
    """
    def __init__(self):
        """
        Locate the SSH binaries on various systems. On Windows we bundle a
        Cygwin build of OpenSSH.
        """
        sixtyFourBitPython = (struct.calcsize('P') * 8 == 64)
        sixtyFourBitOperatingSystem = sixtyFourBitPython or \
            (sys.platform.startswith("win") and win32process.IsWow64Process())
        if "HOME" not in os.environ:
            os.environ["HOME"] = os.path.expanduser('~')
        if sixtyFourBitOperatingSystem:
            winOpensshDir = r"win64\openssh-7.3p1-cygwin-2.6.0"
        else:
            winOpensshDir = r"win32\openssh-7.3p1-cygwin-2.8.0"
        if hasattr(sys, "frozen"):
            baseDir = os.path.dirname(sys.executable)
        else:
            baseDir = os.path.dirname(pkgutil.get_loader("mydata").filename)
            winOpensshDir = os.path.join("resources", winOpensshDir)
        if sys.platform.startswith("win"):
            baseDir = os.path.join(baseDir, winOpensshDir)
            self.preferToUseShellInSubprocess = False
            binarySuffix = ".exe"
            dotSshDir = os.path.join(
                baseDir, "home", getpass.getuser(), ".ssh")
            if not os.path.exists(dotSshDir):
                os.makedirs(dotSshDir)
        else:
            # Using subprocess's shell=True below should be reviewed.
            # Don't change it without testing quoting of special characters.
            baseDir = "/usr/"
            self.preferToUseShellInSubprocess = True
            binarySuffix = ""

        binBaseDir = os.path.join(baseDir, "bin")
        self.ssh = os.path.join(binBaseDir, "ssh" + binarySuffix)
        self.scp = os.path.join(binBaseDir, "scp" + binarySuffix)
        self.sshKeyGen = os.path.join(binBaseDir, "ssh-keygen" + binarySuffix)
        self.mkdir = os.path.join(binBaseDir, "mkdir" + binarySuffix)
        self.cat = os.path.join(binBaseDir, "cat" + binarySuffix)

    @staticmethod
    def DoubleQuote(string):
        """
        Return double-quoted string
        """
        return '"' + string.replace('"', r'\"') + '"'

    @staticmethod
    def DoubleQuoteRemotePath(string):
        """
        Return double-quoted remote path, escaping double quotes,
        backticks and dollar signs
        """
        path = string.replace('"', r'\"')
        path = path.replace('`', r'\\`')
        path = path.replace('$', r'\\$')
        return '"%s"' % path

    @staticmethod
    def DefaultSshOptions():
        """
        Returns default SSH options
        """
        return [
            "-oPasswordAuthentication=no",
            "-oNoHostAuthenticationForLocalhost=yes",
            "-oStrictHostKeyChecking=no",
            "-oConnectTimeout=%s" % CONNECTION_TIMEOUT
        ]


class KeyPair(object):
    """
    Represents an SSH key-pair, e.g. (~/.ssh/MyData, ~/.ssh/MyData.pub)
    """
    def __init__(self, privateKeyFilePath, publicKeyFilePath):
        self.privateKeyFilePath = privateKeyFilePath
        self.publicKeyFilePath = publicKeyFilePath
        self._publicKey = None
        self._fingerprint = None
        self.keyType = None

    def ReadPublicKey(self):
        """
        Read public key, including "ssh-rsa "
        """
        if self.publicKeyFilePath is not None and \
                os.path.exists(self.publicKeyFilePath):
            with open(self.publicKeyFilePath, "r") as pubKeyFile:
                return pubKeyFile.read()
        else:
            raise SshException("Couldn't access MyData.pub in ~/.ssh/")

    def Delete(self):
        """
        Delete SSH keypair

        Only used by tests
        """
        try:
            os.unlink(self.privateKeyFilePath)
            if self.publicKeyFilePath is not None:
                os.unlink(self.publicKeyFilePath)
        except:
            logger.error(traceback.format_exc())
            return False

        return True

    @property
    def publicKey(self):
        """
        Return public key as string
        """
        if self._publicKey is None:
            self._publicKey = self.ReadPublicKey()
        return self._publicKey

    def ReadFingerprintAndKeyType(self):
        """
        Use "ssh-keygen -yl -f privateKeyFile" to extract the fingerprint
        and key type.  This only works if the public key file exists.
        If the public key file doesn't exist, we will generate it from
        the private key file using "ssh-keygen -y -f privateKeyFile".

        On Windows, we're using OpenSSH 7.1p1, and since OpenSSH
        version 6.8, ssh-keygen requires -E md5 to get the fingerprint
        in the old MD5 Hexadecimal format.
        http://www.openssh.com/txt/release-6.8
        Eventually we could switch to the new format, but then MyTardis
        administrators would need to re-approve Uploader Registration
        Requests because of the fingerprint mismatches.
        See the UploaderModel class's ExistingUploadToStagingRequest
        method in mydata.models.uploader
        """
        if not os.path.exists(self.privateKeyFilePath):
            raise PrivateKeyDoesNotExist("Couldn't find valid private key in "
                                         "%s" % self.privateKeyFilePath)
        if self.publicKeyFilePath is None:
            self.publicKeyFilePath = self.privateKeyFilePath + ".pub"
        if not os.path.exists(self.publicKeyFilePath):
            with open(self.publicKeyFilePath, "w") as pubKeyFile:
                pubKeyFile.write(self.publicKey)

        if sys.platform.startswith('win'):
            cmdList = [OPENSSH.sshKeyGen, "-E", "md5",
                       "-yl", "-f", self.privateKeyFilePath]
        else:
            cmdList = [OPENSSH.sshKeyGen, "-yl", "-f", self.privateKeyFilePath]
        logger.debug(" ".join(cmdList))
        proc = subprocess.Popen(cmdList,
                                stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                startupinfo=DEFAULT_STARTUP_INFO,
                                creationflags=DEFAULT_CREATION_FLAGS)
        stdout, _ = proc.communicate()
        if proc.returncode != 0:
            raise SshException(stdout)

        fingerprint = None
        keyType = None
        if stdout is not None:
            sshKeyGenOutComponents = stdout.split(" ")
            if len(sshKeyGenOutComponents) > 1:
                fingerprint = sshKeyGenOutComponents[1]
                if fingerprint.upper().startswith("MD5:"):
                    fingerprint = fingerprint[4:]
            if len(sshKeyGenOutComponents) > 3:
                keyType = sshKeyGenOutComponents[-1]\
                    .strip().strip('(').strip(')')

        return fingerprint, keyType

    @property
    def fingerprint(self):
        """
        Return public key fingerprint
        """
        if self._fingerprint is None:
            self._fingerprint, self.keyType = self.ReadFingerprintAndKeyType()
        return self._fingerprint


def FindKeyPair(keyName="MyData", keyPath=None):
    """
    Find an SSH key pair
    """
    if keyPath is None:
        keyPath = os.path.join(os.path.expanduser('~'), ".ssh")
    if os.path.exists(os.path.join(keyPath, keyName)):
        with open(os.path.join(keyPath, keyName)) as keyFile:
            for line in keyFile:
                if re.search(r"BEGIN .* PRIVATE KEY", line):
                    privateKeyFilePath = os.path.join(keyPath, keyName)
                    publicKeyFilePath = os.path.join(keyPath, keyName + ".pub")
                    if not os.path.exists(publicKeyFilePath):
                        publicKeyFilePath = None
                    return KeyPair(privateKeyFilePath, publicKeyFilePath)
    raise PrivateKeyDoesNotExist("Couldn't find valid private key in %s"
                                 % os.path.join(keyPath, keyName))


def NewKeyPair(keyName=None, keyPath=None, keyComment=None):
    """
    Create an RSA key-pair in ~/.ssh for use with SSH and SCP.
    """
    if keyName is None:
        keyName = "MyData"
    if keyPath is None:
        keyPath = os.path.join(os.path.expanduser('~'), ".ssh")
    if keyComment is None:
        keyComment = "MyData Key"
    privateKeyFilePath = os.path.join(keyPath, keyName)
    publicKeyFilePath = privateKeyFilePath + ".pub"

    dotSshDir = os.path.join(os.path.expanduser('~'), ".ssh")
    if not os.path.exists(dotSshDir):
        os.makedirs(dotSshDir)

    if sys.platform.startswith('win'):
        quotedPrivateKeyFilePath = \
            OpenSSH.DoubleQuote(GetCygwinPath(privateKeyFilePath))
    else:
        quotedPrivateKeyFilePath = OpenSSH.DoubleQuote(privateKeyFilePath)
    cmdList = \
        [OpenSSH.DoubleQuote(OPENSSH.sshKeyGen),
         "-f", quotedPrivateKeyFilePath,
         "-N", '""',
         "-C", OpenSSH.DoubleQuote(keyComment)]
    cmd = " ".join(cmdList)
    logger.debug(cmd)
    proc = subprocess.Popen(cmd,
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            shell=True,
                            startupinfo=DEFAULT_STARTUP_INFO,
                            creationflags=DEFAULT_CREATION_FLAGS)
    stdout, _ = proc.communicate()

    if stdout is None or str(stdout).strip() == "":
        raise SshException("Received unexpected EOF from ssh-keygen.")
    elif "Your identification has been saved" in stdout:
        return KeyPair(privateKeyFilePath, publicKeyFilePath)
    elif "already exists" in stdout:
        raise SshException("Private key file \"%s\" already exists."
                           % privateKeyFilePath)
    else:
        raise SshException(stdout)


def SshServerIsReady(username, privateKeyFilePath,
                     host, port):
    """
    Check if SSH server is ready
    """
    if sys.platform.startswith("win"):
        privateKeyFilePath = GetCygwinPath(privateKeyFilePath)

    if sys.platform.startswith("win"):
        cmdAndArgs = [OpenSSH.DoubleQuote(OPENSSH.ssh),
                      "-p", str(port),
                      "-i", OpenSSH.DoubleQuote(privateKeyFilePath),
                      "-l", username,
                      host,
                      OpenSSH.DoubleQuote("echo Ready")]
    else:
        cmdAndArgs = [OpenSSH.DoubleQuote(OPENSSH.ssh),
                      "-p", str(port),
                      "-i", OpenSSH.DoubleQuote(privateKeyFilePath),
                      "-l", username,
                      host,
                      OpenSSH.DoubleQuote("echo Ready")]
    cmdAndArgs[1:1] = OpenSSH.DefaultSshOptions()
    cmdString = " ".join(cmdAndArgs)
    logger.debug(cmdString)
    proc = subprocess.Popen(cmdString,
                            shell=OPENSSH.preferToUseShellInSubprocess,
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            startupinfo=DEFAULT_STARTUP_INFO,
                            creationflags=DEFAULT_CREATION_FLAGS)
    stdout, _ = proc.communicate()
    returncode = proc.returncode
    if returncode != 0:
        logger.error(stdout)

    return returncode == 0


def UploadFile(filePath, fileSize, username, privateKeyFilePath,
               host, port, remoteFilePath, progressCallback,
               uploadModel):
    """
    Upload a file to staging using SCP.

    Ignore bytes uploaded previously, because MyData is no longer
    chunking files, so with SCP, we will always upload the whole
    file.
    """
    bytesUploaded = 0
    progressCallback(bytesUploaded, fileSize, message="Uploading...")

    if sys.platform.startswith("win"):
        UploadFileFromWindows(filePath, fileSize, username,
                              privateKeyFilePath, host, port,
                              remoteFilePath, progressCallback,
                              uploadModel)
    else:
        UploadFileFromPosixSystem(filePath, fileSize, username,
                                  privateKeyFilePath, host, port,
                                  remoteFilePath, progressCallback,
                                  uploadModel)


def UploadFileFromPosixSystem(filePath, fileSize, username, privateKeyFilePath,
                              host, port, remoteFilePath, progressCallback,
                              uploadModel):
    """
    Upload file using SCP.
    """
    # pylint: disable=too-many-branches
    # pylint: disable=too-many-statements
    # pylint: disable=too-many-locals
    cipher = SETTINGS.miscellaneous.cipher
    progressPollInterval = SETTINGS.miscellaneous.progressPollInterval
    monitoringProgress = threading.Event()
    uploadModel.startTime = datetime.now()
    MonitorProgress(progressPollInterval, uploadModel,
                    fileSize, monitoringProgress, progressCallback)
    remoteDir = os.path.dirname(remoteFilePath)
    quotedRemoteDir = OpenSSH.DoubleQuoteRemotePath(remoteDir)
    if remoteDir not in REMOTE_DIRS_CREATED:
        mkdirCmdAndArgs = \
            [OpenSSH.DoubleQuote(OPENSSH.ssh),
             "-p", port,
             "-n",
             "-c", cipher,
             "-i", OpenSSH.DoubleQuote(privateKeyFilePath),
             "-l", username,
             host,
             OpenSSH.DoubleQuote("mkdir -p %s" % quotedRemoteDir)]
        mkdirCmdAndArgs[1:1] = OpenSSH.DefaultSshOptions()
        mkdirCmdString = " ".join(mkdirCmdAndArgs)
        logger.debug(mkdirCmdString)
        if not sys.platform.startswith("linux"):
            mkdirProcess = \
                subprocess.Popen(mkdirCmdString,
                                 shell=OPENSSH.preferToUseShellInSubprocess,
                                 stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT,
                                 startupinfo=DEFAULT_STARTUP_INFO,
                                 creationflags=DEFAULT_CREATION_FLAGS)
            stdout, _ = mkdirProcess.communicate()
            if mkdirProcess.returncode != 0:
                raise SshException(stdout, mkdirProcess.returncode)
        else:
            stdout, stderr, returncode = \
                linuxsubprocesses.ERRAND_BOY_TRANSPORT.run_cmd(mkdirCmdString)
            if returncode != 0:
                raise SshException(stderr, returncode)
        REMOTE_DIRS_CREATED[remoteDir] = True

    if ShouldCancelUpload(uploadModel):
        logger.debug("UploadFileFromPosixSystem: Aborting upload "
                     "for %s" % filePath)
        return

    maxThreads = SETTINGS.advanced.maxUploadThreads
    remoteDir = os.path.dirname(remoteFilePath)
    if SETTINGS.miscellaneous.useNoneCipher:
        cipherString = "-oNoneEnabled=yes -oNoneSwitch=yes"
    else:
        cipherString = "-c %s" % cipher
    scpCommandString = \
        '%s %s -v -P %s -i %s %s %s "%s@%s:\\"%s\\""' \
        % (OpenSSH.DoubleQuote(OPENSSH.scp),
           " ".join(OpenSSH.DefaultSshOptions()),
           port,
           privateKeyFilePath,
           cipherString,
           OpenSSH.DoubleQuote(filePath),
           username, host,
           remoteDir
           .replace('`', r'\\`')
           .replace('$', r'\\$'))
    logger.debug(scpCommandString)
    if not sys.platform.startswith("linux"):
        scpUploadProcess = subprocess.Popen(
            scpCommandString,
            shell=OPENSSH.preferToUseShellInSubprocess,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            startupinfo=DEFAULT_STARTUP_INFO,
            creationflags=DEFAULT_CREATION_FLAGS)
        uploadModel.scpUploadProcessPid = scpUploadProcess.pid
        while True:
            poll = scpUploadProcess.poll()
            if poll is not None:
                break
            time.sleep(SLEEP_FACTOR * maxThreads)
        stdout, _ = scpUploadProcess.communicate()
        returncode = scpUploadProcess.returncode
        if returncode != 0:
            raise ScpException(stdout, scpCommandString, returncode)
    else:
        with linuxsubprocesses.ERRAND_BOY_TRANSPORT.get_session() as session:
            ebSubprocess = session.subprocess
            if sys.platform.startswith("linux"):
                preexecFunction = os.setpgrp
            else:
                preexecFunction = None
            scpUploadProcess = \
                ebSubprocess.Popen(scpCommandString, shell=True,
                                   close_fds=True, preexec_fn=preexecFunction)
            uploadModel.status = UploadStatus.IN_PROGRESS
            uploadModel.scpUploadProcessPid = scpUploadProcess.pid

            while True:
                poll = scpUploadProcess.poll()
                if poll is not None:
                    break
                time.sleep(SLEEP_FACTOR * maxThreads)
            stdout, stderr = scpUploadProcess.communicate()
            returncode = scpUploadProcess.returncode
            if returncode != 0:
                raise ScpException(stderr, scpCommandString, returncode)

    latestUpdateTime = datetime.now()
    uploadModel.SetLatestTime(latestUpdateTime)
    bytesUploaded = fileSize
    progressCallback(bytesUploaded, fileSize)
    return


REMOTE_DIRS_CREATED = dict()


def UploadFileFromWindows(filePath, fileSize, username,
                          privateKeyFilePath, host, port, remoteFilePath,
                          progressCallback, uploadModel):
    """
    Upload file using SCP.
    """
    # pylint: disable=too-many-statements
    # pylint: disable=too-many-locals
    uploadModel.startTime = datetime.now()
    maxThreads = SETTINGS.advanced.maxUploadThreads
    progressPollInterval = SETTINGS.miscellaneous.progressPollInterval
    monitoringProgress = threading.Event()
    MonitorProgress(progressPollInterval, uploadModel,
                    fileSize, monitoringProgress, progressCallback)
    cipher = SETTINGS.miscellaneous.cipher
    remoteDir = os.path.dirname(remoteFilePath)
    quotedRemoteDir = OpenSSH.DoubleQuoteRemotePath(remoteDir)
    if remoteDir not in REMOTE_DIRS_CREATED:
        mkdirCmdAndArgs = \
            [OpenSSH.DoubleQuote(OPENSSH.ssh),
             "-p", port,
             "-n",
             "-c", cipher,
             "-i", OpenSSH.DoubleQuote(privateKeyFilePath),
             "-l", username,
             host,
             OpenSSH.DoubleQuote("mkdir -p %s" % quotedRemoteDir)]
        mkdirCmdAndArgs[1:1] = OpenSSH.DefaultSshOptions()
        mkdirCmdString = " ".join(mkdirCmdAndArgs)
        logger.debug(mkdirCmdString)
        mkdirProcess = \
            subprocess.Popen(mkdirCmdString,
                             shell=OPENSSH.preferToUseShellInSubprocess,
                             stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT,
                             startupinfo=DEFAULT_STARTUP_INFO,
                             creationflags=DEFAULT_CREATION_FLAGS)
        stdout, _ = mkdirProcess.communicate()
        if mkdirProcess.returncode != 0:
            raise SshException(stdout, mkdirProcess.returncode)
        REMOTE_DIRS_CREATED[remoteDir] = True

    if ShouldCancelUpload(uploadModel):
        logger.debug("UploadFileFromWindows: Aborting upload "
                     "for %s" % filePath)
        return

    remoteDir = os.path.dirname(remoteFilePath)
    if SETTINGS.miscellaneous.useNoneCipher:
        cipherString = "-oNoneEnabled=yes -oNoneSwitch=yes"
    else:
        cipherString = "-c %s" % cipher
    scpCommandString = \
        '%s %s -v -P %s -i %s %s %s "%s@%s:\\"%s/\\""' \
        % (OpenSSH.DoubleQuote(OPENSSH.scp),
           " ".join(OpenSSH.DefaultSshOptions()),
           port,
           OpenSSH.DoubleQuote(GetCygwinPath(privateKeyFilePath)),
           cipherString,
           OpenSSH.DoubleQuote(GetCygwinPath(filePath)),
           username, host,
           remoteDir
           .replace('`', r'\\`')
           .replace('$', r'\\$'))
    logger.debug(scpCommandString)
    scpUploadProcess = subprocess.Popen(
        scpCommandString,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        startupinfo=DEFAULT_STARTUP_INFO,
        creationflags=DEFAULT_CREATION_FLAGS)
    uploadModel.scpUploadProcessPid = scpUploadProcess.pid
    uploadModel.status = UploadStatus.IN_PROGRESS
    while True:
        poll = scpUploadProcess.poll()
        if poll is not None:
            break
        time.sleep(SLEEP_FACTOR * maxThreads)
    stdout, _ = scpUploadProcess.communicate()
    if scpUploadProcess.returncode != 0:
        raise ScpException(stdout, scpCommandString,
                           scpUploadProcess.returncode)
    bytesUploaded = fileSize
    progressCallback(bytesUploaded, fileSize)
    return


def GetCygwinPath(path):
    """
    Converts "C:\\path\\to\\file" to "/cygdrive/C/path/to/file".
    """
    realpath = os.path.realpath(path)
    match = re.search(r"^(\S):(.*)", realpath)
    if match:
        return "/cygdrive/" + match.groups()[0] + \
            match.groups()[1].replace("\\", "/")
    else:
        raise Exception("OpenSSH.GetCygwinPath: %s doesn't look like "
                        "a valid path." % path)


def CleanUpScpAndSshProcesses():
    """
    SCP can leave orphaned SSH processes which need to be cleaned up.
    On Windows, we bundle our own SSH binary with MyData, so we can
    check that the absolute path of the SSH executable to be terminated
    matches MyData's SSH path.  On other platforms, we can use proc.cmdline()
    to ensure that the SSH process we're killing uses MyData's private key.
    """
    privateKeyPath = SETTINGS.uploaderModel.sshKeyPair.privateKeyFilePath
    for proc in psutil.process_iter():
        try:
            if proc.exe() == OPENSSH.ssh or proc.exe() == OPENSSH.scp:
                try:
                    if privateKeyPath in proc.cmdline() or \
                            sys.platform.startswith("win"):
                        proc.kill()
                except:
                    pass
        except psutil.AccessDenied:
            pass
        except psutil.ZombieProcess:
            # Process has completed execution but hasn't been removed from
            # the process table yet.  Usually the process will be removed
            # shortly after this exception is caused, so no action is needed.
            pass


# Singleton instance of OpenSSH class:
OPENSSH = OpenSSH()
