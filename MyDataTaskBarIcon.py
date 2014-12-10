import wx
import os
import sys
import webbrowser


class MyDataTaskBarIcon(wx.TaskBarIcon):
    def __init__(self, frame, settingsModel):
        """Constructor"""
        wx.TaskBarIcon.__init__(self)
        self.frame = frame
        self.settingsModel = settingsModel

        # img = wx.Image("icon_048.png", wx.BITMAP_TYPE_ANY)
        img = wx.Image("favicon.ico", wx.BITMAP_TYPE_ANY)
        bmp = wx.BitmapFromImage(img)
        self.icon = wx.EmptyIcon()
        self.icon.CopyFromBitmap(bmp)

        self.SetIcon(self.icon, "MyData")

        # Mouse event handling set up in InstrumentApp class's OnInit method.
        # self.Bind(wx.EVT_TASKBAR_LEFT_DOWN, self.OnTaskBarLeftClick)

    def OnTaskBarActivate(self, evt):
        """"""
        pass

    def OnTaskBarClose(self, evt):
        """
        Destroy the taskbar icon and frame from the taskbar icon itself
        """
        self.frame.Close()

    def CreatePopupMenu(self):
        self.menu = wx.Menu()

        ms = wx.MenuItem(self.menu, wx.NewId(), "MyTardis Sync")
        self.menu.AppendItem(ms)
        self.Bind(wx.EVT_MENU, self.OnMyTardisSync, ms)

        self.menu.AppendSeparator()

        mcp = wx.MenuItem(self.menu, wx.NewId(), "MyData Control Panel")
        self.menu.AppendItem(mcp)
        self.Bind(wx.EVT_MENU, self.OnMyDataControlPanel, mcp)

        self.menu.AppendSeparator()

        mh = wx.MenuItem(self.menu, wx.NewId(), "MyData Help")
        self.menu.AppendItem(mh)
        self.Bind(wx.EVT_MENU, self.OnMyDataHelp, mh)

        self.menu.AppendSeparator()

        mh = wx.MenuItem(self.menu, wx.NewId(), "Exit MyData")
        self.menu.AppendItem(mh)
        self.Bind(wx.EVT_MENU, self.OnExit, mh)

        return self.menu

    def OnMyDataControlPanel(self, event):
        self.frame.Restore()
        self.frame.Raise()

    def OnMyTardisSync(self, event):
        wx.GetApp().OnRefresh(event)

    def OnMyDataHelp(self, event):
        new = 2  # Open in a new tab, if possible
        url = "https://github.com/wettenhj/mydata/blob/master/User%20Guide.md"
        webbrowser.open(url, new=new)

    def OnExit(self, event):
        message = "Are you sure you want to close MyData?\n\n" \
            "Any uploads currently in progress will be terminated immediately." 
        confirmationDialog = \
            wx.MessageDialog(None, message, "MyData",
                             wx.YES | wx.NO | wx.ICON_QUESTION)
        okToExit = confirmationDialog.ShowModal()
        if okToExit == wx.ID_YES:
            if not self.settingsModel.RunningInBackgroundMode():
                os._exit(0)
            cmd = "Exit MyData.exe"
            if sys.platform.startswith("win"):
                import win32com.shell.shell as shell
                shell.ShellExecuteEx(lpVerb='runas', lpFile=cmd,
                                     lpParameters="")
            elif sys.platform.startswith("darwin"):
                returncode = os.system("osascript -e "
                                       "'do shell script "
                                       "\"echo Exiting MyData\" "
                                       "with administrator privileges'")
                if returncode != 0:
                    raise Exception("Failed to get admin privileges.")
            os._exit(0)
