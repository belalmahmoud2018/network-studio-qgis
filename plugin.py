import os

try:
    from qgis.PyQt.QtGui import QAction  # Qt6
except ImportError:
    from qgis.PyQt.QtWidgets import QAction  # Qt5

from qgis.PyQt.QtGui import QIcon

MENU = "&Network Studio"
TOOLBAR = "Network Studio"


class NetworkStudioPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.toolbar = None
        self.dock = None

    def initGui(self):
        icon = QIcon(os.path.join(os.path.dirname(__file__), "icon.png"))
        self.action = QAction(icon, "Network Studio", self.iface.mainWindow())
        self.action.triggered.connect(lambda _checked=False: self.run())
        self.iface.addPluginToMenu(MENU, self.action)
        self.toolbar = self.iface.addToolBar(TOOLBAR)
        self.toolbar.setObjectName("NetworkStudioToolbar")
        self.toolbar.addAction(self.action)
        self.toolbar.setVisible(True)

    def unload(self):
        if self.dock is not None:
            self.dock.close()
            self.dock.deleteLater()
            self.dock = None
        if self.action is not None:
            self.iface.removePluginMenu(MENU, self.action)
        if self.toolbar is not None:
            self.iface.mainWindow().removeToolBar(self.toolbar)
            self.toolbar.deleteLater()
            self.toolbar = None
        self.action = None

    def run(self):
        """Open the window (floating, non-modal) or bring it to the front."""
        if self.dock is None:
            from .dock import NetworkDock

            self.dock = NetworkDock(self.iface)
        self.dock.show()
        self.dock.raise_()
        self.dock.activateWindow()
