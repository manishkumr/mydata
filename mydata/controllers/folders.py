"""
The main controller class for managing datafile verifications
and uploads from each of the folders in the Folders view.
"""
import sys
import time
import threading
# For Python3, this will change to "from queue import Queue":
from Queue import Queue
import traceback
import datetime

import requests

import wx
import wx.lib.newevent
import wx.dataview

import mydata.events as mde
from ..settings import SETTINGS
from ..models.experiment import ExperimentModel
from ..models.dataset import DatasetModel
from ..logs import logger
from ..utils import BeginBusyCursorIfRequired
from ..utils import EndBusyCursorIfRequired
from ..utils.openssh import CleanUpScpAndSshProcesses
from .uploads import UploadMethod
from .uploads import UploadDatafileRunnable
from .verifications import VerifyDatafileRunnable

if sys.platform.startswith("linux"):
    from ..linuxsubprocesses import StartErrandBoy


class FoldersController(object):
    # pylint: disable=too-many-public-methods
    # pylint: disable=too-many-instance-attributes
    # pylint: disable=too-many-statements
    """
    The main controller class for managing datafile verifications
    and uploads from each of the folders in the Folders view.
    """
    def __init__(self, notifyWindow, foldersModel, foldersView, usersModel,
                 verificationsModel, uploadsModel):
        self.notifyWindow = notifyWindow
        self.foldersModel = foldersModel
        self.foldersView = foldersView
        self.usersModel = usersModel
        self.verificationsModel = verificationsModel
        self.uploadsModel = uploadsModel

        self.shuttingDown = threading.Event()
        self.showingErrorDialog = threading.Event()
        self.lastErrorMessage = None
        self.showingWarningDialog = threading.Event()
        self._canceled = threading.Event()
        self._failed = threading.Event()
        self._started = threading.Event()
        self._completed = threading.Event()

        self.finishedCountingVerifications = dict()
        self.finishedCountingThreadingLock = threading.Lock()
        self.finishedScanningForDatasetFolders = threading.Event()
        self.verificationsQueue = None
        self.lastErrorMessageThreadingLock = threading.Lock()
        self.getOrCreateExpThreadingLock = threading.Lock()
        self.verifyDatafileRunnable = None
        self.uploadsQueue = None
        self.uploadDatafileRunnable = None
        self.numVerificationsToBePerformed = 0
        self.numVerificationsToBePerformedLock = threading.Lock()
        self.uploadsAcknowledged = 0
        self.uploadMethod = UploadMethod.HTTP_POST

        # These will get overwritten in InitForUploads, but we need
        # to initialize them here, so that ShutDownUploadThreads()
        # can be called.
        self.numVerificationWorkerThreads = 0
        self.verificationWorkerThreads = []
        self.numUploadWorkerThreads = 0
        self.uploadWorkerThreads = []

        self.testRun = False

        # pylint: disable=invalid-name
        self.ShowMessageDialogEvent, self.EVT_SHOW_MESSAGE_DIALOG = \
            mde.NewEvent(self.notifyWindow, self.ShowMessageDialog)
        self.ShutdownUploadsEvent, self.EVT_SHUTDOWN_UPLOADS = \
            mde.NewEvent(self.notifyWindow,
                         self.ShutDownUploadThreads)

        # The event type IDs (EVT_...) are used for logging in
        # mydata/events/__init__.py's PostEvent method:
        self.DidntFindDatafileOnServerEvent, \
                self.EVT_DIDNT_FIND_FILE_ON_SERVER = \
            mde.NewEvent(self.notifyWindow, self.UploadDatafile)
        self.FoundIncompleteStagedEvent, self.EVT_FOUND_INCOMPLETE_STAGED = \
            mde.NewEvent(self.notifyWindow, self.UploadDatafile)

        self.FoundVerifiedDatafileEvent, self.EVT_FOUND_VERIFIED = \
            mde.NewEvent(self.notifyWindow,
                         self.CountCompletedUploadsAndVerifications)
        self.FoundFullSizeStagedEvent, self.EVT_FOUND_FULLSIZE_STAGED = \
            mde.NewEvent(self.notifyWindow,
                         self.CountCompletedUploadsAndVerifications)
        self.FoundUnverifiedNoDfosDatafileEvent, \
                self.EVT_FOUND_UNVERIFIED_NO_DFOS = \
            mde.NewEvent(self.notifyWindow,
                         self.CountCompletedUploadsAndVerifications)
        # If we're not using staged uploads, we can't retry the upload, because
        # the DataFile has already been created and we don't want to trigger
        # a Duplicate Key error, so we just need to wait for the file to be
        # verified:
        self.FoundUnverifiedUnstagedEvent, \
                self.EVT_FOUND_UNVERIFIED_UNSTAGED = \
            mde.NewEvent(self.notifyWindow,
                         self.CountCompletedUploadsAndVerifications)

        self.UploadCompleteEvent, self.EVT_UPLOAD_COMPLETE = \
            mde.NewEvent(self.notifyWindow,
                         self.CountCompletedUploadsAndVerifications)
        self.UploadFailedEvent, self.EVT_UPLOAD_FAILED = \
            mde.NewEvent(self.notifyWindow,
                         self.CountCompletedUploadsAndVerifications)

    @property
    def started(self):
        """
        Return thread-safe flag indicated whether uploads have started
        """
        return self._started.isSet()

    @started.setter
    def started(self, started):
        """
        Set thread-safe flag indicated whether uploads have started
        """
        if started:
            self._started.set()
        else:
            self._started.clear()

    @property
    def canceled(self):
        """
        Return thread-safe flag indicated whether uploads have been canceled
        """
        return self._canceled.isSet()

    @canceled.setter
    def canceled(self, canceled):
        """
        Set thread-safe flag indicated whether uploads have been canceled
        """
        if canceled:
            self._canceled.set()
        else:
            self._canceled.clear()

    @property
    def failed(self):
        """
        Return thread-safe flag indicated whether uploads have failed
        """
        return self._failed.isSet()

    @failed.setter
    def failed(self, failed):
        """
        Set thread-safe flag indicated whether uploads have failed
        """
        if failed:
            self._failed.set()
        else:
            self._failed.clear()

    @property
    def completed(self):
        """
        Return thread-safe flag indicated whether uploads have completed
        """
        return self._completed.isSet()

    @completed.setter
    def completed(self, completed):
        """
        Set thread-safe flag indicated whether uploads have completed
        """
        if completed:
            self._completed.set()
        else:
            self._completed.clear()

    def IsShuttingDown(self):
        """
        Return True if folder scans and uploads are shutting down
        """
        return self.shuttingDown.isSet()

    def SetShuttingDown(self, shuttingDown=True):
        """
        Set to True if folder scans and uploads are shutting down
        """
        if shuttingDown:
            self.shuttingDown.set()
        else:
            self.shuttingDown.clear()

    def IsShowingErrorDialog(self):
        """
        Return True if MyData is showing an error dialog
        """
        return self.showingErrorDialog.isSet()

    def SetShowingErrorDialog(self, showingErrorDialog=True):
        """
        Set to True if MyData is showing an error dialog
        """
        if showingErrorDialog:
            self.showingErrorDialog.set()
        else:
            self.showingErrorDialog.clear()

    def GetLastErrorMessage(self):
        """
        Return last error message
        """
        return self.lastErrorMessage

    def SetLastErrorMessage(self, message):
        """
        Set last error message
        """
        self.lastErrorMessageThreadingLock.acquire()
        self.lastErrorMessage = message
        self.lastErrorMessageThreadingLock.release()

    def ShowMessageDialog(self, event):
        """
        Display a message dialog.

        Sometimes multiple threads can encounter the same exception
        at around the same time.  The first thread's exception leads
        to a modal error dialog, which blocks the events queue, so
        the next thread's (identical) show message dialog event doesn't
        get caught until after the first message dialog has been closed.
        In this case, we check if we already showed an error dialog with
        the same message.
        """
        if self.IsShowingErrorDialog():
            logger.warning("Refusing to show message dialog for message "
                           "\"%s\" because we are already showing an error "
                           "dialog." % event.message)
            return
        elif event.message == self.GetLastErrorMessage():
            logger.warning("Refusing to show message dialog for message "
                           "\"%s\" because we already showed an error "
                           "dialog with the same message." % event.message)
            return
        self.SetLastErrorMessage(event.message)
        if event.icon == wx.ICON_ERROR:
            self.SetShowingErrorDialog(True)
        dlg = wx.MessageDialog(None, event.message, event.title,
                               wx.OK | event.icon)
        try:
            wx.EndBusyCursor()
            needToRestartBusyCursor = True
        except:
            needToRestartBusyCursor = False
        if wx.PyApp.IsMainLoopRunning():
            dlg.ShowModal()
        else:
            sys.stderr.write("%s\n" % event.message)
        app = wx.GetApp()
        if needToRestartBusyCursor and not self.IsShuttingDown() \
                and hasattr(app, "PerformingLookupsAndUploads") \
                and app.PerformingLookupsAndUploads():
            BeginBusyCursorIfRequired()
        if event.icon == wx.ICON_ERROR:
            self.SetShowingErrorDialog(False)

    def UploadDatafile(self, event):
        """
        Called in response to DidntFindDatafileOnServerEvent or
        UnverifiedDatafileOnServerEvent.

        This method runs in the main thread, so it shouldn't do anything
        time-consuming or blocking, unless it launches another thread.
        Because this method adds upload tasks to a queue, it is important
        to note that if the queue has a maxsize set, then an attempt to
        add something to the queue could block the GUI thread, making the
        application appear unresponsive.
        """
        folderModel = event.folderModel
        dfi = event.dataFileIndex
        existingUnverifiedDatafile = \
            getattr(event, "existingUnverifiedDatafile", False)

        if self.testRun:
            if existingUnverifiedDatafile:
                message = "NEEDS RE-UPLOADING: %s" \
                    % folderModel.GetDataFileRelPath(dfi)
            else:
                message = "NEEDS UPLOADING: %s" \
                    % folderModel.GetDataFileRelPath(dfi)
            self.uploadsAcknowledged += 1
            logger.testrun(message)
            self.CountCompletedUploadsAndVerifications(event=None)
            return

        if folderModel not in self.uploadDatafileRunnable:
            self.uploadDatafileRunnable[folderModel] = {}

        bytesUploadedPreviously = \
            getattr(event, "bytesUploadedPreviously", None)
        verificationModel = getattr(event, "verificationModel", None)
        self.uploadDatafileRunnable[folderModel][dfi] = \
            UploadDatafileRunnable(self, self.foldersModel, folderModel,
                                   dfi, self.uploadsModel,
                                   existingUnverifiedDatafile,
                                   verificationModel,
                                   bytesUploadedPreviously)
        if wx.PyApp.IsMainLoopRunning():
            self.uploadsQueue.put(
                self.uploadDatafileRunnable[folderModel][dfi])
        else:
            self.uploadDatafileRunnable[folderModel][dfi].Run()
        self.CountCompletedUploadsAndVerifications(event=None)

    def InitForUploads(self):
        """
        Initialize folders controller in preparation for uploads
        """
        # pylint: disable=too-many-branches
        app = wx.GetApp()
        if hasattr(app, "TestRunRunning"):
            self.testRun = app.TestRunRunning()
        else:
            self.testRun = False
        self.started = True
        self.canceled = False
        self.failed = False
        self.completed = False
        self.verificationsModel.DeleteAllRows()
        self.uploadsModel.DeleteAllRows()
        self.uploadsModel.SetStartTime(datetime.datetime.now())
        self.verifyDatafileRunnable = {}
        self.verificationsQueue = Queue()
        self.numVerificationWorkerThreads = \
            SETTINGS.miscellaneous.maxVerificationThreads
        self.verificationWorkerThreads = []

        if wx.PyApp.IsMainLoopRunning():
            for i in range(self.numVerificationWorkerThreads):
                thread = threading.Thread(
                    name="VerificationWorkerThread-%d" % (i + 1),
                    target=self.VerificationWorker)
                self.verificationWorkerThreads.append(thread)
                thread.start()
        self.uploadDatafileRunnable = {}
        self.uploadsQueue = Queue()
        self.numUploadWorkerThreads = SETTINGS.advanced.maxUploadThreads
        self.uploadMethod = UploadMethod.HTTP_POST
        self.getOrCreateExpThreadingLock = threading.Lock()
        self.lastErrorMessage = None
        self.SetShowingErrorDialog(False)

        if sys.platform.startswith("linux"):
            StartErrandBoy()

        try:
            SETTINGS.uploaderModel.RequestStagingAccess()
            uploadToStagingRequest = SETTINGS.uploadToStagingRequest
        except Exception as err:
            # MyData app could be missing from MyTardis server.
            logger.error(traceback.format_exc())
            mde.PostEvent(
                self.ShowMessageDialogEvent(
                    title="MyData",
                    message=str(err),
                    icon=wx.ICON_ERROR))
            return
        message = None
        if uploadToStagingRequest is None:
            message = "Couldn't determine whether uploads to " \
                      "staging have been approved.  " \
                      "Falling back to HTTP POST."
        elif uploadToStagingRequest.approved:
            logger.info("Uploads to staging have been approved.")
            self.uploadMethod = UploadMethod.VIA_STAGING
        else:
            message = \
                "Uploads to MyTardis's staging area require " \
                "approval from your MyTardis administrator.\n\n" \
                "A request has been sent, and you will be contacted " \
                "once the request has been approved. Until then, " \
                "MyData will upload files using HTTP POST, and will " \
                "only upload one file at a time.\n\n" \
                "HTTP POST is generally only suitable for small " \
                "files (up to 100 MB each)."
        if message:
            logger.warning(message)
            mde.PostEvent(
                self.ShowMessageDialogEvent(
                    title="MyData",
                    message=message,
                    icon=wx.ICON_WARNING))
            self.uploadMethod = UploadMethod.HTTP_POST
        if self.uploadMethod == UploadMethod.HTTP_POST and \
                self.numUploadWorkerThreads > 1:
            logger.warning(
                "Using HTTP POST, so setting "
                "numUploadWorkerThreads to 1, "
                "because urllib2 is not thread-safe.")
            self.numUploadWorkerThreads = 1

        self.uploadWorkerThreads = []
        if wx.PyApp.IsMainLoopRunning():
            for i in range(self.numUploadWorkerThreads):
                thread = threading.Thread(
                    name="UploadWorkerThread-%d" % (i + 1),
                    target=self.UploadWorker, args=())
                self.uploadWorkerThreads.append(thread)
                thread.start()

        self.finishedScanningForDatasetFolders = threading.Event()
        self.numVerificationsToBePerformed = 0
        self.finishedCountingVerifications = dict()

    def FinishedScanningForDatasetFolders(self):
        """
        At this point, we know that FoldersModel's
        ScanFolders method has finished populating
        self.foldersModel with dataset folders.
        """
        self.finishedScanningForDatasetFolders.set()
        logger.debug("Finished scanning for dataset folders.")
        while len(self.finishedCountingVerifications.keys()) < \
                self.foldersModel.GetCount():
            time.sleep(0.01)
        self.CountCompletedUploadsAndVerifications(event=None)

    def StartUploadsForFolder(self, folderModel):
        """
        Start uploads for the specified folder
        """
        # pylint: disable=too-many-branches
        try:
            self.finishedCountingThreadingLock.acquire()
            self.finishedCountingVerifications[folderModel] = threading.Event()
            self.finishedCountingThreadingLock.release()
            app = wx.GetApp()
            if self.IsShuttingDown() or \
                    (hasattr(app, "ShouldAbort") and app.ShouldAbort()):
                return
            self.numVerificationsToBePerformedLock.acquire()
            self.numVerificationsToBePerformed += folderModel.GetNumFiles()
            self.numVerificationsToBePerformedLock.release()
            logger.debug(
                "StartUploadsForFolder: Starting verifications "
                "and uploads for folder: " + folderModel.folderName)
            if self.IsShuttingDown() or \
                    (hasattr(app, "ShouldAbort") and app.ShouldAbort()):
                return
            try:
                try:
                    self.getOrCreateExpThreadingLock.acquire()
                    experimentModel = ExperimentModel\
                        .GetOrCreateExperimentForFolder(folderModel,
                                                        self.testRun)
                except Exception as err:
                    logger.error(traceback.format_exc())
                    mde.PostEvent(
                        self.ShowMessageDialogEvent(
                            title="MyData",
                            message=str(err),
                            icon=wx.ICON_ERROR))
                    return
                finally:
                    self.getOrCreateExpThreadingLock.release()
                folderModel.experimentModel = experimentModel
                try:
                    folderModel.datasetModel = DatasetModel\
                        .CreateDatasetIfNecessary(folderModel, self.testRun)
                except Exception as err:
                    logger.error(traceback.format_exc())
                    mde.PostEvent(
                        self.ShowMessageDialogEvent(
                            title="MyData",
                            message=str(err),
                            icon=wx.ICON_ERROR))
                    return
                self.VerifyDatafiles(folderModel)
            except requests.exceptions.ConnectionError as err:
                logger.error(str(err))
                return
            except ValueError:
                logger.error("Failed to retrieve experiment "
                             "for folder " + folderModel.folderName)
                logger.error(traceback.format_exc())
                return
            if experimentModel is None and not self.testRun:
                logger.error("Failed to acquire a MyTardis "
                             "experiment to store data in for "
                             "folder " + folderModel.folderName)
                return
            if self.IsShuttingDown() or \
                    (hasattr(app, "ShouldAbort") and app.ShouldAbort()):
                return
            self.finishedCountingThreadingLock.acquire()
            self.finishedCountingVerifications[folderModel].set()
            self.finishedCountingThreadingLock.release()
            if self.foldersModel.GetRowCount() == 0 or \
                    self.numVerificationsToBePerformed == 0:
                # For the case of zero folders or zero files, we
                # can't use the usual triggers (e.g. datafile
                # upload complete) to determine when to check if
                # we have finished:
                self.CountCompletedUploadsAndVerifications(event=None)
            # End: for row in range(0, self.foldersModel.GetRowCount())
        except:
            logger.error(traceback.format_exc())

    def UploadWorker(self):
        # Could be moved to uploads controller
        """
        One worker per thread
        By default, up to 5 threads can run simultaneously
        for uploading local data files to
        the MyTardis server.
        """
        while True:
            if self.IsShuttingDown():
                return
            task = self.uploadsQueue.get()
            if task is None:
                return
            try:
                task.Run()
            except ValueError as err:
                if str(err) == "I/O operation on closed file":
                    logger.info(
                        "Ignoring closed file exception - it is normal "
                        "to encounter these exceptions while canceling "
                        "uploads.")
                    self.uploadsQueue.task_done()
                    return
                else:
                    logger.error(traceback.format_exc())
                    self.uploadsQueue.task_done()
                    return
            except:
                logger.error(traceback.format_exc())
                self.uploadsQueue.task_done()
                return

    def VerificationWorker(self):
        # Could be moved to verifications controller
        """
        One worker per thread.
        By default, up to 5 threads can run simultaneously
        for verifying whether local data files exist on
        the MyTardis server.
        """
        while True:
            if self.IsShuttingDown():
                return
            task = self.verificationsQueue.get()
            if task is None:
                break
            try:
                task.Run()
            except ValueError as err:
                if str(err) == "I/O operation on closed file":
                    logger.info(
                        "Ignoring closed file exception - it is normal "
                        "to encounter these exceptions while canceling "
                        "uploads.")
                    self.verificationsQueue.task_done()
                    return
                else:
                    logger.error(traceback.format_exc())
                    self.verificationsQueue.task_done()
                    return
            except:
                logger.error(traceback.format_exc())
                self.verificationsQueue.task_done()
                return

    def CountCompletedUploadsAndVerifications(self, event):
        """
        Check if we have finished uploads and verifications,
        and if so, call ShutDownUploadThreads
        """
        # pylint: disable=unused-argument
        if self.completed or self.canceled:
            return

        numVerificationsCompleted = self.verificationsModel.GetCompletedCount()

        uploadsToBePerformed = self.uploadsModel.GetRowCount() + \
            self.uploadsQueue.qsize()

        uploadsCompleted = self.uploadsModel.GetCompletedCount()
        uploadsFailed = self.uploadsModel.GetFailedCount()
        uploadsProcessed = uploadsCompleted + uploadsFailed

        if hasattr(wx.GetApp(), "GetMainFrame"):
            if numVerificationsCompleted == \
                    self.numVerificationsToBePerformed \
                    and uploadsToBePerformed > 0:
                message = "Uploaded %d of %d files." % \
                    (uploadsCompleted, uploadsToBePerformed)
            else:
                message = "Looked up %d of %d files on server." % \
                    (numVerificationsCompleted,
                     self.numVerificationsToBePerformed)
            wx.GetApp().GetMainFrame().SetStatusMessage(message)

        finishedVerificationCounting = \
            self.finishedScanningForDatasetFolders.isSet()
        for folder in self.finishedCountingVerifications:
            if not self.finishedCountingVerifications[folder]:
                finishedVerificationCounting = False
                break

        if numVerificationsCompleted == self.numVerificationsToBePerformed \
                and finishedVerificationCounting \
                and (uploadsProcessed == uploadsToBePerformed or
                     self.testRun and
                     self.uploadsAcknowledged == uploadsToBePerformed):
            logger.debug("All datafile verifications and uploads "
                         "have completed.")
            logger.debug("Shutting down upload and verification threads.")
            mde.PostEvent(self.ShutdownUploadsEvent(completed=True))
        elif not wx.PyApp.IsMainLoopRunning() and self.testRun and \
                finishedVerificationCounting:
            mde.PostEvent(self.ShutdownUploadsEvent(completed=True))

    def ShutDownUploadThreads(self, event=None):
        """
        Shut down upload threads
        """
        # pylint: disable=too-many-branches
        if self.IsShuttingDown() or self.completed or self.canceled:
            return
        self.SetShuttingDown(True)
        app = wx.GetApp()
        SETTINGS.CloseVerifiedDatafilesCache()
        if hasattr(app, "PerformingLookupsAndUploads") and \
                not app.PerformingLookupsAndUploads():
            # This means StartUploadsForFolder was never called
            EndBusyCursorIfRequired()
            if hasattr(app, "toolbar"):
                app.EnableTestAndUploadToolbarButtons()
                app.SetShouldAbort(False)
                if self.testRun:
                    app.testRunFrame.saveButton.Enable()
            message = "No folders were found to upload from."
            logger.info(message)
            if hasattr(app, "GetMainFrame"):
                app.GetMainFrame().SetStatusMessage(message)
            self.completed = True
            self.SetShuttingDown(False)
            return
        message = "Shutting down upload threads..."
        logger.info(message)
        if hasattr(app, "GetMainFrame"):
            app.GetMainFrame().SetStatusMessage(message)
        if hasattr(event, "failed") and event.failed:
            self.failed = True
            self.uploadsModel.CancelRemaining()
        elif hasattr(event, "completed") and event.completed:
            self.completed = True
        else:
            self.canceled = True
            self.uploadsModel.CancelRemaining()
        logger.debug("Shutting down FoldersController upload worker threads.")
        for _ in range(self.numUploadWorkerThreads):
            self.uploadsQueue.put(None)
        if self.uploadMethod == UploadMethod.VIA_STAGING:
            # SCP can leave orphaned SSH processes which need to be
            # cleaned up.
            # Give each UploadModel instance's Cancel() method a chance to
            # terminate its SCP process first:
            time.sleep(0.1)
            CleanUpScpAndSshProcesses()
        for thread in self.uploadWorkerThreads:
            thread.join()
        logger.debug("Shutting down FoldersController verification "
                     "worker threads.")
        for _ in range(self.numVerificationWorkerThreads):
            self.verificationsQueue.put(None)
        for thread in self.verificationWorkerThreads:
            thread.join()

        self.verifyDatafileRunnable = {}
        self.uploadDatafileRunnable = {}

        if self.testRun:
            self.LogTestRunSummary()

        if self.failed:
            message = "Data scans and uploads failed."
        elif self.canceled:
            message = "Data scans and uploads were canceled."
        elif self.uploadsModel.GetFailedCount() > 0:
            message = \
                "Data scans and uploads completed with " \
                "%d failed upload(s)." % self.uploadsModel.GetFailedCount()
        elif self.completed:
            if self.uploadsModel.GetCompletedCount() > 0:
                message = "Data scans and uploads completed successfully."
                elapsedTime = self.uploadsModel.GetElapsedTime()
                if elapsedTime and not self.testRun:
                    averageSpeedMBs = \
                        (float(self.uploadsModel.GetCompletedSize()) /
                         1000000.0 / elapsedTime.total_seconds())
                    if averageSpeedMBs >= 1.0:
                        averageSpeed = "%3.1f MB/s" % averageSpeedMBs
                    else:
                        averageSpeed = \
                            "%3.1f KB/s" % (averageSpeedMBs * 1000.0)
                    message += "  Average speed: %s" % averageSpeed
            else:
                message = "No new files were found to upload."
        else:
            message = "Data scans and uploads appear to have " \
                "completed successfully."
        logger.info(message)
        if hasattr(app, "GetMainFrame"):
            app.GetMainFrame().SetStatusMessage(message)
        if self.testRun:
            logger.testrun(message)

        if hasattr(app, "toolbar"):
            app.EnableTestAndUploadToolbarButtons()
            app.SetShouldAbort(False)
            if self.testRun:
                app.testRunFrame.saveButton.Enable()
        if hasattr(wx.GetApp(), "SetPerformingLookupsAndUploads"):
            wx.GetApp().SetPerformingLookupsAndUploads(False)
        self.SetShuttingDown(False)
        if hasattr(app, "SetTestRunRunning"):
            app.SetTestRunRunning(False)

        EndBusyCursorIfRequired()

        logger.debug("")

    def LogTestRunSummary(self):
        """
        Log summary of test run to display in Test Run frame
        """
        numVerificationsCompleted = \
            self.verificationsModel.GetCompletedCount()
        numVerifiedUploads = \
            self.verificationsModel.GetFoundVerifiedCount()
        numFilesNotFoundOnServer = \
            self.verificationsModel.GetNotFoundCount()
        numFullSizeUnverifiedUploads = \
            self.verificationsModel.GetFoundUnverifiedFullSizeCount()
        numIncompleteUploads = \
            self.verificationsModel.GetFoundUnverifiedNotFullSizeCount()
        numFailedLookups = self.verificationsModel.GetFailedCount()
        logger.testrun("")
        logger.testrun("SUMMARY")
        logger.testrun("")
        logger.testrun("Files looked up on server: %s"
                       % numVerificationsCompleted)
        logger.testrun("Files verified on server: %s" % numVerifiedUploads)
        logger.testrun("Files not found on server: %s"
                       % numFilesNotFoundOnServer)
        logger.testrun("Files unverified (but full size) on server: %s"
                       % numFullSizeUnverifiedUploads)
        logger.testrun("Files unverified (and incomplete) on server: %s"
                       % numIncompleteUploads)
        logger.testrun("Failed lookups: %s" % numFailedLookups)
        logger.testrun("")

    def VerifyDatafiles(self, folderModel):
        """
        Verify datafiles in the specified folder
        """
        if folderModel not in self.verifyDatafileRunnable:
            self.verifyDatafileRunnable[folderModel] = []
        for dfi in range(0, folderModel.numFiles):
            if self.IsShuttingDown():
                return
            self.verifyDatafileRunnable[folderModel].append(
                VerifyDatafileRunnable(
                    self, self.foldersModel, folderModel, dfi, self.testRun))
            if wx.PyApp.IsMainLoopRunning():
                self.verificationsQueue\
                    .put(self.verifyDatafileRunnable[folderModel][dfi])
            else:
                self.verifyDatafileRunnable[folderModel][dfi].Run()
