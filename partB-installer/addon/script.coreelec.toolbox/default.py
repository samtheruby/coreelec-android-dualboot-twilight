import xbmcaddon
from resources.lib.gui_main_menu import MainMenu


def main():
    win = MainMenu("toolbox_menu.xml", xbmcaddon.Addon().getAddonInfo("path"))
    win.doModal()
    del win


if __name__ == '__main__':
    main()
