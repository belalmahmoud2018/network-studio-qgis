def classFactory(iface):
    from .plugin import NetworkStudioPlugin
    return NetworkStudioPlugin(iface)
