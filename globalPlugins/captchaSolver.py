import threading
import cPickle
import os
import json
import base64
import io
import httplib
import time
from urllib import urlencode
import globalPluginHandler
import wx
import gui
from logHandler import log
import scriptHandler
import addonHandler
import queueHandler
import ui
import globalVars
import api
import speech
import controlTypes

addonHandler.initTranslation()

MAX_INSTRUCTION_LENGTH = 140 # Maximum text length of instruction for the worker
FILE_CONFIG_PATH = os.path.join(globalVars.appArgs.configPath, "captchaSolverSettings.pickle")
RUCAPTCHA_PROFILE_URL = "https://rucaptcha.com/auth/login"
ADDON_URL = addonHandler.getCodeAddon().manifest.get("url")

ERRORS = {
	"ERROR_CONNECTING_TO_SERVER": _("Error connecting to server. Please check your Internet connection"),
	"ERROR_WRONG_USER_KEY": _("API key is not specified"),
	"ERROR_KEY_DOES_NOT_EXIST": _("Used a non-existent API key"),
	"ERROR_ZERO_BALANCE": _("The balance of your account is zero"),
	"ERROR_NO_SLOT_AVAILABLE": _("The current recognition rate is higher than the maximum set in the settings of Your account. Either on the server queue builds up and employees do not have time to disassemble it, repeat the sending captcha after 5 seconds"),
	"ERROR_ZERO_CAPTCHA_FILESIZE": _("Size of the captcha is less than 100 bytes"),
	"ERROR_TOO_BIG_CAPTCHA_FILESIZE": _("Size of the captcha more than 100 KB"),
	"ERROR_IP_NOT_ALLOWED": _("In Your account you have configured restrictions based on IP from which you can make requests. And the IP from which the request is not included in the allowed list"),
	"IP_BANNED": _("IP address from which the request is blocked because of frequent requests with various incorrect API keys. The lock is released in an hour"),
	"ERROR_CAPTCHA_UNSOLVABLE": _("Captcha could not solve 3 different employee. Money for this image come back to balance"),
	"ERROR_BAD_DUPLICATES": _("The error appears when 100 percent recognition. Has been used the maximum number of attempts, but the required number of identical answers has not been received"),
	"ERROR_CAPTCHAIMAGE_BLOCKED": _("This captcha can not be recognized"),
}

conf = {
	"graphicOnly": True,
	"regsense": False,
	"sizeReport": False,
	"textInstruction": False,
	"language": 0,
	"key": "",
}

class SettingsDialog(gui.SettingsDialog):
	title = _("Captcha Solver Settings")

	def makeSettings(self, sizer):
		settingsSizerHelper = gui.guiHelper.BoxSizerHelper(self, sizer=sizer)

		self.graphicOnly = wx.CheckBox(self, label=_("Recognize only &graphic objects"))
		self.graphicOnly.SetValue(conf["graphicOnly"])
		settingsSizerHelper.addItem(self.graphicOnly)

		self.regsense = wx.CheckBox(self, label=_("&Case sensitive recognition"))
		self.regsense.SetValue(conf["regsense"])
		settingsSizerHelper.addItem(self.regsense)

		self.sizeReport = wx.CheckBox(self, label=_("Report image &size"))
		self.sizeReport.SetValue(conf["sizeReport"])
		settingsSizerHelper.addItem(self.sizeReport)

		self.textInstruction = wx.CheckBox(self, label=_("Send &text instruction"))
		self.textInstruction.SetValue(conf["textInstruction"])
		settingsSizerHelper.addItem(self.textInstruction)

		self.language = settingsSizerHelper.addLabeledControl(_("Image &language:"), wx.Choice, choices=[_("Undefined"), _("Only Cyrillic alphabet"), _("Only Latin alphabet")])
		self.language.SetSelection(conf["language"])

		self.key = settingsSizerHelper.addLabeledControl(_("API &key:"), wx.TextCtrl, value=conf["key"])

	def postInit(self):
		self.graphicOnly.SetFocus()

	def onOk(self, event):
		conf["graphicOnly"] = self.graphicOnly.IsChecked()
		conf["regsense"] = self.regsense.IsChecked()
		conf["sizeReport"] = self.sizeReport.IsChecked()
		conf["textInstruction"] = self.textInstruction.IsChecked()
		conf["language"] = self.language.GetSelection()
		conf["key"] = self.key.GetValue()

		# Saves global conf into config file
		try:
			with open(FILE_CONFIG_PATH, "wb") as fileConfig:
				cPickle.dump(conf, fileConfig, cPickle.HIGHEST_PROTOCOL)
		except (IOError, OSError) as e:
			gui.messageBox(e.strerror, _("Error saving settings"), style=wx.OK | wx.ICON_ERROR)

		super(SettingsDialog, self).onOk(event)

class RucaptchaError(Exception): pass

class RucaptchaRequest(threading.Thread):

	def __init__(self, callback, **kwargs):
		super(RucaptchaRequest, self).__init__()
		self.__callback = callback
		self.__kwargs = kwargs
		self.__host = "rucaptcha.com"
		self.__connection = httplib.HTTPSConnection(self.__host)
		self.daemon = True
		self.start()

	def run(self):
		try:
			request = self._request(**self.__kwargs)
		except (httplib.socket.gaierror, httplib.ssl.SSLError, httplib.socket.timeout):
			request = RuntimeError("ERROR_CONNECTING_TO_SERVER")
		except Exception as e:
			request = e
		finally:
			self.__connection.close()
			wx.CallAfter(self.__callback, request)

	def _request(self, **kwargs):
		kwargs["json"] = 1
		kwargs["key"] = conf["key"].encode("utf-8")

		if "body" not in kwargs:
			path = "/res.php?" + urlencode(kwargs)
			return self._HTTPRequest("GET", path, None)

		kwargs["soft_id"] = 1665 # ID of CaptchaSolver from rucaptcha.com. Used for statistics
		kwargs["regsense"] = int(conf["regsense"])
		kwargs["language"] = conf["language"]
		kwargs["method"] = "base64"
		kwargs["body"] = base64.b64encode(kwargs["body"])

		captchaID = self._HTTPRequest("POST", "/in.php", urlencode(kwargs))

		queueHandler.queueFunction(queueHandler.eventQueue, speech.cancelSpeech)
		queueHandler.queueFunction(queueHandler.eventQueue, ui.message, _("Captcha successfully sent to the recognition. You will be notified when the result will be ready"))

		while True:
			time.sleep(2)
			try:
				return self._request(action="get", id=captchaID)
			except RucaptchaError as e:
				if e.message != "CAPCHA_NOT_READY": raise e

	def _HTTPRequest(self, method, path, body):
		headers = {"Host": self.__host}
		if body:
			headers["Content-Type"] = "application/x-www-form-urlencoded"

		self.__connection.request(method, path, body, headers)
		response = self.__connection.getresponse()
		if response.status != httplib.OK:
			raise RuntimeError("{} {}".format(response.status, response.reason))

		responseDict = json.load(response)
		request = responseDict.get("request")

		if responseDict.get("status") != 1:
			raise RucaptchaError(request)
		return request

class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	scriptCategory = _("Captcha Solver")

	def __init__(self):
		super(GlobalPlugin, self).__init__()
		if globalVars.appArgs.secure: return

		# Updates global conf from config file
		try:
			with open(FILE_CONFIG_PATH, "rb") as fileConfig:
				conf.update(cPickle.load(fileConfig))
		except Exception:
			pass

		# Creates submenu of addon
		menu_CaptchaSolver = wx.Menu()
		item = menu_CaptchaSolver.Append(wx.ID_ANY, _("Settings..."))
		gui.mainFrame.sysTrayIcon.Bind(wx.EVT_MENU, lambda evt: gui.mainFrame._popupSettingsDialog(SettingsDialog), item)
		item = menu_CaptchaSolver.Append(wx.ID_ANY, _("Account balance..."))
		gui.mainFrame.sysTrayIcon.Bind(wx.EVT_MENU, lambda evt: RucaptchaRequest(self.balanceDialog, action="getbalance"), item)
		item = menu_CaptchaSolver.Append(wx.ID_ANY, _("Profile on rucaptcha.com"))
		gui.mainFrame.sysTrayIcon.Bind(wx.EVT_MENU, lambda evt: os.startfile(RUCAPTCHA_PROFILE_URL), item)
		item = menu_CaptchaSolver.Append(wx.ID_ANY, _("Addon webpage"))
		gui.mainFrame.sysTrayIcon.Bind(wx.EVT_MENU, lambda evt: os.startfile(ADDON_URL), item)

		gui.mainFrame.sysTrayIcon.toolsMenu.AppendMenu(wx.ID_ANY, _("Captcha Solver"), menu_CaptchaSolver)

	def getErrorDescription(self, error):
		description = ERRORS.get(error.message)
		if not isinstance(description, basestring):
			description = _("Unknown CaptchaSolver error. For details, see the NVDA log")
		log.error(u"{0}: {1}".format(type(error).__name__, error))
		return description

	def balanceDialog(self, request):
		if isinstance(request, Exception):
			gui.messageBox(self.getErrorDescription(request), _("Error getting balance"), style=wx.OK | wx.ICON_ERROR)
			return

		gui.messageBox(_("{:.2f} rubles").format(float(request)), _("Your account balance"))

	def captchaHandler(self, request):
		if isinstance(request, Exception):
			ui.message(self.getErrorDescription(request))
			return

		api.copyToClip(request)
		ui.message(_("Captcha solved successfully! The result copied to the clipboard"))

	def balanceHandler(self, request):
		if isinstance(request, Exception):
			ui.message(self.getErrorDescription(request))
			return

		ui.message(_("Balance: {:.2f}").format(float(request)))

	def _creator(self, **kwargs):
		if conf["textInstruction"]:
			dlg = wx.TextEntryDialog(gui.mainFrame, _("Instruction text (maximum {length} characters):").format(length=MAX_INSTRUCTION_LENGTH), _("Sending text instruction"))
			dlg.SetMaxLength(MAX_INSTRUCTION_LENGTH)
			gui.mainFrame.prePopup()
			status = dlg.ShowModal()
			gui.mainFrame.postPopup()
			textInstruction = dlg.GetValue()
			dlg.Destroy()
			if status != wx.ID_OK: return
			kwargs["textinstructions"] = textInstruction.encode("utf-8")
		RucaptchaRequest(self.captchaHandler, **kwargs)

	def script_startRecognition(self, gesture):
		if globalVars.appArgs.secure:
			ui.message(_("Action cannot be performed because NVDA running on secure desktop"))
			return

		obj = api.getNavigatorObject()

		if obj.role != controlTypes.ROLE_GRAPHIC and conf["graphicOnly"]:
			ui.message(_("This object is not a graphical element"))
			return

		if controlTypes.STATE_OFFSCREEN in obj.states:
			ui.message(_("Captcha off screen"))
			return

		try:
			x, y, width, height = obj.location
		except Exception:
			ui.message(_("Captcha has no location"))
			return

		if conf["sizeReport"] and scriptHandler.getLastScriptRepeatCount() != 1:
			ui.message(_("Size: {0} X {1} pixels").format(width, height))
			return

		bmp = wx.EmptyBitmap(width, height)
		mem = wx.MemoryDC(bmp)
		mem.Blit(0, 0, width, height, wx.ScreenDC(), x, y)
		image = bmp.ConvertToImage()
		body = io.BytesIO()
		image.SaveFile(body, wx.BITMAP_TYPE_PNG)

		wx.CallAfter(self._creator, body=body.getvalue())
	script_startRecognition.__doc__ = _("Starts the recognition process")

	def script_getBalance(self, gesture):
		if globalVars.appArgs.secure:
			ui.message(_("Action cannot be performed because NVDA running on secure desktop"))
			return

		RucaptchaRequest(self.balanceHandler, action="getbalance")
	script_getBalance.__doc__ = _("Report account balance")

	def script_showSettingsDialog(self, gesture):
		if globalVars.appArgs.secure:
			ui.message(_("Action cannot be performed because NVDA running on secure desktop"))
			return

		gui.mainFrame._popupSettingsDialog(SettingsDialog)
	script_showSettingsDialog.__doc__ = _("Show the settings dialog")
