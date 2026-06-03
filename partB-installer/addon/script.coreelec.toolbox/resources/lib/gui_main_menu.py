# gui_main_menu.py -- CoreELEC Toolbox main menu
import xbmcgui
import xbmc
import xbmcaddon

from resources.lib import bt_sync, boot_default, wifi_mac

ADDON = xbmcaddon.Addon()

# (label, action)
MENU_ITEMS = [
    ("Sync Bluetooth remotes from Android", "bt_sync"),
    ("Set default boot OS", "boot_default"),
    ("Fix WiFi MAC address", "wifi_mac"),
]

DESCRIPTIONS = {
    "bt_sync": (
        "Import Bluetooth remote pairings from Android so a remote paired in Android also "
        "works in CoreELEC -- no re-pairing. Only INPUT devices (remotes, keyboards) are "
        "synced; audio devices (earbuds/headphones) are skipped. First pair the remote in "
        "Android, then run this. Requires the Android-side export (installed with the dual-boot)."
    ),
    "boot_default": (
        "Choose which OS a normal power-on boots: Android or CoreELEC. The other OS stays one "
        "step away -- from CoreELEC use 'reboot to eMMC/nand' to reach Android; from Android use "
        "the Reboot-to-CoreELEC app. If the chosen OS ever fails to boot, the device falls back "
        "to the other automatically."
    ),
    "wifi_mac": (
        "Set the CoreELEC WiFi MAC address. NEEDED ONLY on devices where CoreELEC shows a wrong "
        "or shared default WiFi MAC. On most boxes CoreELEC already reads the correct unique MAC "
        "from the WiFi chip and this is unnecessary. You can use the MAC captured from Android, "
        "enter one manually, or reset to the chip's hardware MAC. Reboot + WiFi re-connect required."
    ),
}


class MainMenu(xbmcgui.WindowXMLDialog):
    def onInit(self):
        self.list = self.getControl(1000)
        self.desc = self.getControl(1001)
        self.menu_items = list(MENU_ITEMS)
        for label, _ in self.menu_items:
            self.list.addItem(xbmcgui.ListItem(label))
        self.update_description()
        xbmc.sleep(150)
        xbmc.executebuiltin("SetFocus(1000)")

    def update_description(self):
        pos = self.list.getSelectedPosition()
        if 0 <= pos < len(self.menu_items):
            _, action = self.menu_items[pos]
            self.desc.setText(DESCRIPTIONS.get(action, ""))

    def onAction(self, action):
        aid = action.getId()
        if aid in (xbmcgui.ACTION_MOVE_UP, xbmcgui.ACTION_MOVE_DOWN,
                   xbmcgui.ACTION_PAGE_UP, xbmcgui.ACTION_PAGE_DOWN):
            self.update_description()
        elif aid in (xbmcgui.ACTION_PREVIOUS_MENU, xbmcgui.ACTION_NAV_BACK):
            self.close()
        super(MainMenu, self).onAction(action)

    def onClick(self, controlId):
        if controlId == 1000:
            i = self.list.getSelectedPosition()
            if 0 <= i < len(self.menu_items):
                self.run_action(self.menu_items[i][1])
        elif controlId == 1500:
            self.close()

    def run_action(self, action):
        if action == "bt_sync":
            bt_sync.run()
        elif action == "boot_default":
            boot_default.run()
        elif action == "wifi_mac":
            wifi_mac.run()
