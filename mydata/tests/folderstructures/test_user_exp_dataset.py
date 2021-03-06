"""
Test ability to scan the Username / Experiment / Dataset folder structure.
"""
from .. import MyDataScanFoldersTester
from .. import ValidateSettingsAndScanFolders


class ScanUserExpDatasetTester(MyDataScanFoldersTester):
    """
    Test ability to scan the Username / Experiment / Dataset folder structure.
    """
    def setUp(self):
        super(ScanUserExpDatasetTester, self).setUp()
        super(ScanUserExpDatasetTester, self).InitializeAppAndFrame(
            'ScanUserExpDatasetTester')

    def test_scan_folders(self):
        """
        Test ability to scan the Username / Experiment / Dataset folder structure.
        """
        self.UpdateSettingsFromCfg("testdataUserExpDataset")
        ValidateSettingsAndScanFolders()
        self.AssertUsers(["testuser1", "testuser2"])
        self.AssertFolders(["Birds", "Flowers"])
        self.AssertNumFiles(5)
