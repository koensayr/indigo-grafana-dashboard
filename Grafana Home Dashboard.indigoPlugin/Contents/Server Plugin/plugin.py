#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright (c) 2018 Mike Lamoureux
#

import indigo
import datetime
import time
import json
import os
from json_adaptor import JSONAdaptor
from influxdb import InfluxDBClient
from influxdb.exceptions import InfluxDBClientError
import fileinput
import sys
import shutil
import subprocess
from subprocess import check_output
import socket
import signal

DEFAULT_POLLING_INTERVAL = 60  # number of seconds between each poll
UPDATE_STATES_LIST = 15 # how frequently (in minutes) to update the state list

DEFAULT_STATES = ["state.onOffState", "state.hvac_state", "onState", "energyCurLevel", "energyAccumTotal", "value.num", "sensorValue", "coolSetpoint", "heatSetpoint", "batteryLevel", "batteryLevel.num"]

class Plugin(indigo.PluginBase):
	def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
		super(Plugin, self).__init__(pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
		indigo.devices.subscribeToChanges()
		indigo.variables.subscribeToChanges()
		self.connection = None
		self.QuietNoGrafanaConfigured = False
		self.QuietConnectionError = False
		self.QuietNoInfluxConfigured = False
		self.InfluxServerPID = None
		self.GrafanaServerPID = None
		self.triggerInfluxReset = False
		self.triggerInfluxAdminReset = False
		self.triggerGrafanaReset = False
		self.InfluxRequireAuth = True

		self.VariableLastUpdatedList = []
		self.DeviceLastUpdatedList = []

		self.FullStateList = []
		self.AvailableStatesUI = []
		self.AvailableIncDevices = []
		self.AvailableExlDevices = []

		self.DeviceIncludeListUI = []
		self.DeviceListExcludeListUI = []

		self.folders = {}
		self.pollingInterval = DEFAULT_POLLING_INTERVAL
		self.connected = False
		self.InfluxServerStatus = "stopped"
		self.GrafanaServerStatus = "stopped"
		self.influxConfigFileLoc = os.getcwd() + '/servers/influxdb/influxdb.conf'
		self.GrafanaConfigFileLoc = os.getcwd() + '/servers/grafana/conf/indigo.ini'

		self.LastConfigRefresh = datetime.datetime.now()

	def startup(self):
		try:
			self.InfluxHost = self.pluginPrefs.get('InfluxHost', 'localhost')
			self.InfluxPort = self.pluginPrefs.get('InfluxPort', '8088')
			self.InfluxHTTPPort = self.pluginPrefs.get('InfluxHTTPPort', '8086')
			self.InfluxUser = self.pluginPrefs.get('InfluxUser', 'indigo')
			self.InfluxPassword = self.pluginPrefs.get('InfluxPassword', 'indigo')
			self.InfluxDB = self.pluginPrefs.get('InfluxDB', 'indigo')
			self.GrafanaPort = self.pluginPrefs.get('GrafanaPort', '3006')
			self.ExternalDB = self.pluginPrefs.get('ExternalDB', False)
			self.GrafanaDataLocation = self.pluginPrefs.get('GrafanaDataLocation', None)
			self.InfluxDataLocation = self.pluginPrefs.get('InfluxDataLocation', None)
			self.DisableGrafana = self.pluginPrefs.get('DisableGrafana', False)
			self.miniumumUpdateFrequency = int(self.pluginPrefs.get("MinimumUpdateFrequency", DEFAULT_POLLING_INTERVAL/60))

			self.ServerDebug = self.pluginPrefs.get("ServerDebug", False)
			self.debug = self.ServerDebug

			self.TransportDebug = self.pluginPrefs.get("TransportDebug", False)
			self.TransportDebugL2 = self.pluginPrefs.get("TransportDebugL2", False)
			self.JSONDebug = self.pluginPrefs.get("JSONDebug", False)

			self.StatesIncludeList = self.pluginPrefs.get("listIncStates", [])
			self.DeviceIncludeList = self.pluginPrefs.get("listIncDevices", [])
			self.DeviceExcludeList = self.pluginPrefs.get("listExclDevices", [])

			if len(self.StatesIncludeList) == 0:
				self.StatesIncludeList = DEFAULT_STATES[:]

			newlist = []
			for item in self.DeviceIncludeList:
				newlist.append(int(item))

			self.DeviceIncludeList = newlist

			self.adaptor = JSONAdaptor(self.logger, self.TransportDebug, self.TransportDebugL2, self.JSONDebug)

		except Exception as e:
			if self.debug:
				self.logger.error("error on startup: " + str(e))
			else:
				self.logger.error("missing proper configuration to start up.")
			pass

		if len(self.StatesIncludeList) == 0:
			self.BuildConfigurationLists()

		self.restartAll()
		self.BuildConfigurationLists()
		self.UpdateAll()

	# called after runConcurrentThread() exits
	def shutdown(self):
		pass

	def checkForUpdates(self):
		self.updater.checkForUpdate()

	def updatePlugin(self):
		self.updater.update()

	def connect(self):
		self.connected = False

		if self.InfluxServerStatus == "started" or self.ExternalDB:		
			if not self.QuietConnectionError:
				indigo.server.log("connecting to InfluxDB... " + self.InfluxHost + ":" + self.InfluxHTTPPort + " using user account: " + self.InfluxUser)

			self.connection = InfluxDBClient(
				host=self.InfluxHost,
				port=int(self.InfluxHTTPPort),
				username=self.InfluxUser,
				password=self.InfluxPassword,
				database=self.InfluxDB)

			try:
				self.connection.create_database(self.InfluxDB)
				self.connection.switch_database(self.InfluxDB)
#				self.connection.create_retention_policy('two_year_policy', '730d', '1')

				indigo.server.log(u'connected to InfluxDB sucessfully...')
				self.connected = True
				self.QuietConnectionError = False
			except:
				if not self.QuietConnectionError:
					self.logger.error("error while connecting to InfluxDB, will continue to try silently in the background.")
					self.QuietConnectionError = True

				self.connected = False

	# send this a dict of what to write
	def SendToInflux(self, tags, what, measurement='device_changes'):
		if not self.connected:
			return

		json_body=[
			{
				'measurement': measurement,
				'tags' : tags,
				'fields':  what
			}
		]

		# don't like my types? ok, fine, what DO you want?
		retrylimit = 30
		unsent = True
		while unsent and retrylimit > 0:
			retrylimit -= 1
			try:
				self.connection.write_points(json_body)
				unsent = False
			except InfluxDBClientError as e:
				#print(str(e))
				field = json.loads(e.content)['error'].split('"')[1]
				#measurement = json.loads(e.content)['error'].split('"')[3]
				retry = json.loads(e.content)['error'].split('"')[4].split()[7]
				if retry == 'integer':
					retry = 'int'
				if retry == 'string':
					retry = 'str'
				# float is already float
				# now we know to try to force this field to this type forever more
				self.adaptor.typecache[field] = retry
				try:
					newcode = '%s("%s")' % (retry, str(json_body[0]['fields'][field]))
					#indigo.server.log(newcode)
					json_body[0]['fields'][field] = eval(newcode)
				except ValueError:
					pass
					#indigo.server.log('One of the columns just will not convert to its previous type. This means the database columns are just plain wrong.')
			except ValueError:
				if self.pluginPrefs.get(u'debug', False):
					indigo.server.log(u'Unable to force a field to the type in Influx - a partial record was still written')
			except Exception as e:
				indigo.server.log("Error while trying to write:")
				indigo.server.log(unicode(e))
		if retrylimit == 0 and unsent:
			if self.pluginPrefs.get(u'debug', False):
				indigo.server.log(u'Unable to force all fields to the types in Influx - a partial record was still written')

	def runConcurrentThread(self):
		self.logger.debug("starting concurrent tread...")
		indigo.server.log("fully initialized and ready...")
		self.sleep(int(self.pollingInterval))

		try:
			# Polling - As far as what is known, there is no subscription method using web standards available from August.
			while True:
				try:

					self.checkServerStatus()

					if self.triggerInfluxAdminReset:
						self.logger.debug("detected that a Influx Admin Account Reset needs to occur...")
						self.triggerInfluxAdminReset = False
						self.triggerInfluxReset = False
						self.CreateInfluxAdmin()

					elif self.triggerInfluxReset and self.triggerGrafanaReset:
						self.logger.debug("detected that a server restart needs to occur...")
						self.triggerInfluxReset = False
						self.triggerGrafanaReset = False
						self.restartAll()

					elif self.triggerInfluxReset:
						self.logger.debug("detected that a influx server restart needs to occur...")
						self.triggerInfluxReset = False
						self.restartInflux()

					elif self.triggerGrafanaReset:
						self.logger.debug("detected that a grafana server restart needs to occur...")
						self.triggerGrafanaReset = False
						self.restartGrafana()

					if self.InfluxServerStatus != "started" and not self.ExternalDB:
						self.logger.debug("no valid InfluxDB server to connect to. InfluxServerStatus: " + str(self.InfluxServerStatus) + ", ExternalDB: " + str(self.ExternalDB))
						if not self.QuietNoInfluxConfigured:
							self.QuietNoInfluxConfigured = True
							self.logger.error("no valid InfluxDB server to connect to; please check plugin config.  Future errors are now silenced.")
						
						self.sleep(int(self.pollingInterval))
						continue

					self.QuietNoInfluxConfigured = False

					if not self.connected:
						self.connect()

					if self.connected:
						self.UpdateAll()

				except Exception as e:
					if self.debug:
						self.logger.error("Error in thread: " + str(e))

					if not self.connected:
						self.connect()
					pass

				self.sleep(int(self.pollingInterval))

		except self.StopThread:
			self.logger.debug("Received StopThread")
			self.StopInfluxServer()
			self.StopGrafanaServer()

	def UpdateAll(self):

		if not self.connected:
			return

		self.logger.debug("running UpdateAll()")

		for dev in indigo.devices:
			needsUpdating = False
			found = False

			if self.TransportDebugL2:
				self.logger.debug("reviewing " + dev.name)

			# if the device is excluded, do nothing
			if dev.id in self.DeviceExcludeList:
				if self.TransportDebug:
					self.logger.debug("device was excluded from InfluxDB update: " + dev.name)
				continue

			for devSearch in self.DeviceLastUpdatedList:
				if devSearch[0] == dev.id:
					found = True

					if devSearch[1] + datetime.timedelta(minutes=self.miniumumUpdateFrequency) < datetime.datetime.now():
						if self.TransportDebug:
							self.logger.debug("minimum update period for device expired: " + dev.name + ", prior update timestamp: " + str(devSearch[1]))

						needsUpdating = True
						devSearch[1] = datetime.datetime.now()

					break

			if not found:
				self.DeviceLastUpdatedList.append([dev.id, dev.lastChanged])

				if datetime.datetime.now() + datetime.timedelta(minutes=UPDATE_STATES_LIST) < self.LastConfigRefresh:
					self.logger.debug("updating the states cache as it has gone stale")
					self.BuildConfigurationLists()

			if needsUpdating:
				self.DeviceToInflux(dev, False)

		for var in indigo.variables:
			needsUpdating = False
			found = False

			for varSearch in self.VariableLastUpdatedList:
				if varSearch[0] == var.id:
					found = True

					if varSearch[1] + datetime.timedelta(minutes=self.miniumumUpdateFrequency) < datetime.datetime.now():
						if self.TransportDebug:
							self.logger.debug("minimum update period for variable expired: " + var.name + ", prior update timestamp: " + str(varSearch[1]))

						needsUpdating = True
						varSearch[1] = datetime.datetime.now()

					break

			if not found:
				self.VariableLastUpdatedList.append([var.id, datetime.datetime.now()])

			if needsUpdating:
				self.VariableToInflux(var)

		self.logger.debug("completed UpdateAll()")

	def deviceUpdated(self, origDev, newDev):
		# call base implementation
		indigo.PluginBase.deviceUpdated(self, origDev, newDev)

		if not self.connected:
			return

		if self.TransportDebugL2:
			self.logger.debug("an update for device " + origDev.name + " is being processed...")

		# If the device is excluded, do nothing
		if newDev.id in self.DeviceExcludeList:
			if self.TransportDebug:
				self.logger.debug("device was excluded from InfluxDB update: " + newDev.name)

			return

		device_was_updated = self.DeviceToInflux(newDev)

		if not device_was_updated:
			if self.JSONDebug:
				self.logger.debug("an update for device " + origDev.name + " resulted in no properties updated...")

			return

		found = False
		for devSearch in self.DeviceLastUpdatedList:
			if devSearch[0] == origDev.name:
				found = True

				devSearch[1] = datetime.datetime.now()
				break

		if not found:
			self.DeviceLastUpdatedList.append([origDev.name, datetime.datetime.now()])

	def restartAll(self):
		self.StopInfluxServer()
		self.StopGrafanaServer()

		if not self.ExternalDB:
			self.StartInfluxServer()
		else:
			self.connect()

		if self.connected and not self.DisableGrafana:
			self.StartGrafanaServer()
			self.QuietNoGrafanaConfigured = False

		if not self.connected and not self.QuietNoGrafanaConfigured:
			self.logger.error("not currently connected to any InfluxDB, so will not start the Grafana Server")
			self.QuietNoGrafanaConfigured = True

		if not self.connected:
			self.logger.debug("not currently connected to any InfluxDB")

	def restartGrafana(self):
		indigo.server.log("######## About to (re) start Grafana.  Please be patient while this happens. ########")
		self.StopGrafanaServer()

		if not self.DisableGrafana:
			self.StartGrafanaServer()

	def restartInflux(self):
		indigo.server.log("######## About to (re) start InfluxDB.  Please be patient while this happens. ########")
		self.StopInfluxServer()

		if not self.ExternalDB:
			self.StartInfluxServer()
		else:
			self.connect()

	def StopInfluxServer(self):
		if self.ExternalDB:
			return

		self.InfluxServerStatus = "stopped"
		self.connected = False

		if self.InfluxServerPID is not None:
			indigo.server.log("shutting down the InfluxDB Server...")
			self.InfluxServerPID.kill()

		self.InfluxServerPID = None

		p = subprocess.Popen(['ps', '-A'], stdout=subprocess.PIPE)
		out, err = p.communicate()
		
		for line in out.splitlines():
			if 'influxd' in line:
				pid = int(line.split(None, 1)[0])
				os.kill(pid, signal.SIGKILL)		

	def StartInfluxServer(self):
		if not os.path.isfile(self.influxConfigFileLoc):
			self.logger.debug("please check your config, config file does not exist")
			return

		if self.InfluxDataLocation is None:
			self.logger.debug("please check your config, data location for Influx does not exist")
			return

		if not os.path.isdir(self.InfluxDataLocation):
			self.logger.debug("the InfluxDB data location does not exist, please check your configuration")
			return

		indigo.server.log ("starting the InfluxDB Server...")
		runInfluxCommand = os.getcwd().replace(" ", "\ ") + "/servers/influxdb/influxd run -config " + self.influxConfigFileLoc.replace(" ", "\ ")

		self.logger.debug("starting the InfluxDB Server using command: " + runInfluxCommand)

		self.InfluxServerPID = subprocess.Popen(runInfluxCommand, shell=True)
		time.sleep(5)

		loopcount = 1
		while loopcount < 10:
			time.sleep(5)
			result = self.checkRunningServer(self.InfluxPort)
			if result:
				indigo.server.log ("InfluxDB server started...")
				self.InfluxServerStatus = "started"

				self.connect()
				return True
				
			loopcount = loopcount + 1

		self.logger.error("error starting the InfluxDB server")
		self.InfluxServerStatus = "stopped"

	def StartGrafanaServer(self):
		if not os.path.isfile(self.GrafanaConfigFileLoc):
			indigo.server.log("cannot start Grafana, please check your config")
			return

		if self.GrafanaDataLocation is None:
			self.logger.debug("please check your config, data location for Grafana does not exist")
			return

		if not os.path.isdir(self.GrafanaDataLocation):
			self.logger.debug("the Grafana data location does not exist, please check your configuration")
			return

		indigo.server.log ("starting the Grafana server...")
		runGrafanaCommand = os.getcwd().replace(" ", "\ ") + "/servers/grafana/grafana-server -homepath " + os.getcwd().replace(" ", "\ ") + "/servers/grafana/" + " -config " + self.GrafanaConfigFileLoc.replace(" ", "\ ")

		self.logger.debug("starting the Grafana server using command: " + runGrafanaCommand)

		self.GrafanaServerPID = subprocess.Popen(runGrafanaCommand, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
		time.sleep(5)

		loopcount = 1
		while loopcount < 3:
			time.sleep(5)
			result = self.checkRunningServer(self.GrafanaPort)
			if result:
				indigo.server.log("######## Grafana server started. ########")
				indigo.server.log ("You can now access your Indigo Home Dashboard via: http://localhost:" + self.GrafanaPort + " (or by using your Mac's IP address from another computer on your local network)")
				self.GrafanaServerStatus = "started"
				return True
				
			loopcount = loopcount + 1

		self.logger.error("error starting the Grafana server")
		self.GrafanaServerStatus = "stopped"

	def StopGrafanaServer(self):
		if self.DisableGrafana:
			return

		self.GrafanaServerStatus = "stopped"

		if self.GrafanaServerPID is not None:
			indigo.server.log("shutting down the Grafana Server...")
			self.GrafanaServerPID.kill()

		self.GrafanaServerPID = None

		p = subprocess.Popen(['ps', '-A'], stdout=subprocess.PIPE)
		out, err = p.communicate()
		
		for line in out.splitlines():
			if 'grafana' in line:
				pid = int(line.split(None, 1)[0])
				os.kill(pid, signal.SIGKILL)		

	def checkServerStatus(self):
		influxResult = self.checkRunningServer(self.InfluxHTTPPort)
		grafanaResult = self.checkRunningServer(self.InfluxHTTPPort)

		if grafanaResult:
			self.GrafanaServerStatus = "started"
		else:
			self.GrafanaServerStatus = "stopped"

		if influxResult:
			self.InfluxServerStatus = "started"
		else:
			self.InfluxServerStatus = "stopped"

	def checkRunningServer(self, port):
		try:			
			sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
			result = sock.connect_ex(("127.0.0.1", int(port)))
			
			if result == 0: return True
			
		except Exception as e:
			self.logger.error(e)	
			
		return False

	def CreateGrafanaConfig(self):
		if not self.DisableGrafana:

			if not os.path.isdir(self.GrafanaDataLocation):
				self.logger.error("the Grafana data location does not exist, please check your configuration")
				return

			if self.GrafanaServerStatus != "stopped":
				self.StopGrafanaServer()

			GrafanaDefaultConfigFileLoc = os.getcwd() + '/servers/grafana/conf/defaults.ini'

			if os.path.isfile(self.GrafanaConfigFileLoc):
				os.remove(self.GrafanaConfigFileLoc)

			shutil.copyfile(GrafanaDefaultConfigFileLoc, self.GrafanaConfigFileLoc)

			for line in fileinput.input(self.GrafanaConfigFileLoc, inplace=True):
				if fileinput.lineno() == 15:
					line = "data = " + self.GrafanaDataLocation + "\n"
				if fileinput.lineno() == 18:
					line = "logs = " + self.GrafanaDataLocation + "/log\n"
				if fileinput.lineno() == 35:
					line = "http_port = " + self.GrafanaPort + "\n"

				sys.stdout.write(line)

			indigo.server.log("config file for Grafana has been updated; server will restart shortly.")

	def DeleteInfluxAdmin(self, user):
		self.logger.debug("droping database user: " + user)
		if self.connected:
			try:
				self.connection.drop_user(user)
			except:
				self.logger.debug("Error while dropping user, likely the user did not exist")
				return False

			return True
		return False

	def CreateInfluxAdmin(self):
		self.StopInfluxServer()

		if self.InfluxRequireAuth:
			self.CreateInfluxConfig(RequireAuth = False)
	
		self.StartInfluxServer()

		self.logger.debug("creating database admin user: " + self.InfluxUser)

		self.connection.create_user(self.InfluxUser, self.InfluxPassword, admin=True)
		self.connection.grant_privilege("all", self.InfluxDB, self.InfluxUser)

		self.CreateInfluxConfig(RequireAuth = True)
		self.StartInfluxServer()

	def CreateInfluxConfig(self, RequireAuth = False):
		if not self.ExternalDB:

			if not os.path.isdir(self.InfluxDataLocation):
				self.logger.error("the InfluxDB data location does not exist, please check your configuration")
				return

			if self.InfluxServerStatus != "stopped":
				self.StopInfluxServer()

			currentSection = "none"
			influxDefaultConfigFileLoc = os.getcwd() + '/servers/influxdb/default.conf'

			if os.path.isfile(self.influxConfigFileLoc):
				os.remove(self.influxConfigFileLoc)

			shutil.copyfile(influxDefaultConfigFileLoc, self.influxConfigFileLoc)

			if self.debug:
				self.logger.debug("loading the Influx configuration file: " + self.influxConfigFileLoc)

			bindAddress = "127.0.0.1:" + self.InfluxPort

			for line in fileinput.input(self.influxConfigFileLoc, inplace=True):

				# section detection

				if line[0] == "[":
					currentSection = line.rstrip()

					self.logger.debug("now editing Influx config section: " + currentSection)

				if fileinput.lineno() == 2 and currentSection == "none" and "bind-address" in line:
					line = "bind-address = \"" + bindAddress + "\"\n"
				elif currentSection == "[meta]" and "dir" in line:
					line = "  dir = \"" + self.InfluxDataLocation + "/meta\"\n"
				elif currentSection == "[data]" and "wal-dir" in line:
					line = "  wal-dir = \"" + self.InfluxDataLocation + "/wal\"\n"
				elif currentSection == "[data]" and "dir" in line:
					line = "  dir = \"" + self.InfluxDataLocation + "/data\"\n"
				elif currentSection == "[http]" and "bind-address" in line:
					line = "  bind-address = \":" + self.InfluxHTTPPort + "\"\n"
				elif RequireAuth and currentSection == "[http]" and "auth-enabled" in line:
					self.logger.debug("influxDB auth-enabled set to true")
					self.InfluxRequireAuth = True
					line = "  auth-enabled = true\n"
				elif not RequireAuth and currentSection == "[http]" and "auth-enabled" in line:
					self.logger.debug("influxDB auth-enabled set to false")
					self.InfluxRequireAuth = False
					line = "  auth-enabled = false\n"

				sys.stdout.write(line)

			indigo.server.log("config file for InfluxDB has been updated; server will restart shortly.")


	def DeviceToInflux(self, dev, updateCheck = True):
		# custom add to influx work
		# tag by folder if present
		tagnames = u'name folderId'.split()

		# if the device is excluded, do nothing
		if dev.id in self.DeviceExcludeList:
			if self.TransportDebug:
				self.logger.debug("device was excluded from InfluxDB update: " + dev.name)
			return False

		if dev.id in self.DeviceIncludeList:
			self.logger.debug("sending entire device to diff_to_json: " + dev.name)
			newjson = self.adaptor.diff_to_json(dev, [], updateCheck)
		else:
			newjson = self.adaptor.diff_to_json(dev, self.StatesIncludeList, updateCheck)

		if newjson == None:
			return False

		newtags = {}
		for tag in tagnames:
			newtags[tag] = unicode(getattr(dev, tag))

		# add a folder name tag
		if hasattr(dev, u'folderId') and dev.folderId != 0:
			newtags[u'folder'] = indigo.devices.folders[dev.folderId].name

		measurement = newjson[u'measurement']
		del newjson[u'measurement']
		self.SendToInflux(tags=newtags, what=newjson, measurement=measurement)

		return True

	def variableUpdated(self, origVar, newVar):
		indigo.PluginBase.variableUpdated(self, origVar, newVar)

		if not self.connected:
			return

		## Log the timestamp for the variable update
		found = False
		for varSearch in self.VariableLastUpdatedList:
			if varSearch[0] == origVar.name:
				found = True

				varSearch[1] = datetime.datetime.now()
				break

		if not found:
			self.VariableLastUpdatedList.append([origVar.name, datetime.datetime.now()])

		self.VariableToInflux(newVar)

	def VariableToInflux(self, var):
		newtags = {u'varname': var.name}
		newjson = {u'name': var.name, u'value': var.value }
		numval = self.adaptor.smart_value(var.value, True)
		if numval != None:
			newjson[u'value.num'] = numval

		self.SendToInflux(tags=newtags, what=newjson, measurement=u'variable_changes')

################### PluginConfig functions. #####################################################

	def closedPrefsConfigUi(self, valuesDict, userCancelled):
		if not userCancelled:
			InfluxServerChanged = False
			GrafanaServerChanged = False
			ExternalInfluxDBServerChanged = False

			self.ServerDebug = valuesDict["ServerDebug"]
			self.debug = self.ServerDebug

			self.TransportDebug = valuesDict["TransportDebug"]
			self.TransportDebugL2 = valuesDict["TransportDebugL2"]
			self.JSONDebug = valuesDict["JSONDebug"]

			self.adaptor.TransportDebug = self.TransportDebug
			self.adaptor.TransportDebugL2 = self.TransportDebugL2
			self.adaptor.JSONDebug = self.JSONDebug

			self.logger.debug("started processing closedPrefsConfigUi")

			if self.ExternalDB != valuesDict['ExternalDB']:
				# If the user changed the External status, and the internal server was previously running, we need to stop the server.
				if self.InfluxServerStatus == "started":
					self.StopInfluxServer()
				
				# this flag is used for the admin accounts
				ExternalInfluxDBServerChanged = True

				self.ExternalDB = valuesDict['ExternalDB']

				# If the user changed from external to internal, refresh Grafana.  Not sure why, validate
				if not self.ExternalDB:
					GrafanaServerChanged = True

			# If the user saved the config and External was selected and the server is stopped, we want to trigger a refresh no matter what was changed.
			if not self.ExternalDB and self.InfluxServerStatus == "stopped":
				InfluxServerChanged = True

			if self.ExternalDB:
				self.InfluxHost = valuesDict['InfluxHost']
			else:
				self.InfluxHost = "localhost"

			if self.InfluxPort != valuesDict['InfluxPort']:
				self.InfluxPort = valuesDict['InfluxPort']
				InfluxServerChanged = True

			if self.InfluxHTTPPort != valuesDict['InfluxHTTPPort']:
				self.InfluxHTTPPort = valuesDict['InfluxHTTPPort']
				InfluxServerChanged = True

			# Here we have to detect if the admin accounts had changed
			if ExternalInfluxDBServerChanged or (not self.ExternalDB and (valuesDict['InfluxUser'] != self.InfluxUser or self.InfluxPassword != valuesDict['InfluxPassword'])):
				self.logger.debug("config for admin user change detected, purging old admin user")
				InfluxServerChanged = True
				self.triggerInfluxAdminReset = True
				self.DeleteInfluxAdmin(self.InfluxUser)
				self.InfluxUser = valuesDict['InfluxUser']
				self.InfluxPassword = valuesDict['InfluxPassword']
	
			if self.InfluxDB != valuesDict['InfluxDB']:
				self.InfluxDB = valuesDict['InfluxDB']
				InfluxServerChanged = True

			if self.InfluxDataLocation != valuesDict["InfluxDataLocation"]:
				self.InfluxDataLocation = valuesDict["InfluxDataLocation"]
				InfluxServerChanged = True

			if self.GrafanaDataLocation != valuesDict["GrafanaDataLocation"]:
				self.GrafanaDataLocation = valuesDict["GrafanaDataLocation"]
				GrafanaServerChanged = True

			if self.GrafanaPort != valuesDict["GrafanaPort"]:
				self.GrafanaPort = valuesDict["GrafanaPort"]
				GrafanaServerChanged = True

			if self.DisableGrafana != valuesDict["DisableGrafana"]:
				if self.GrafanaServerStatus == "started":
					self.StopGrafanaServer()

				self.DisableGrafana = valuesDict["DisableGrafana"]

				if not self.DisableGrafana:
					GrafanaServerChanged = True

			if not self.DisableGrafana and self.GrafanaServerStatus == "stopped":
				GrafanaServerChanged = True

			if InfluxServerChanged and not self.ExternalDB:
				self.logger.debug("identified that config properties for the InfluxDB Server have changed")
				self.CreateInfluxConfig()
				self.triggerInfluxReset = True

			if GrafanaServerChanged:
				self.logger.debug("identified that config properties for the Grafana Server have changed")
				self.CreateGrafanaConfig()
				self.triggerGrafanaReset = True

			# if a change was made to the Influx credentials and we are using a external server, simply reconnect
			if self.ExternalDB and InfluxServerChanged:
				self.connect()

			self.miniumumUpdateFrequency = int(valuesDict["MinimumUpdateFrequency"])

			# Reset the Exclude List to the selected devices that are selected in the list box
			self.DeviceExcludeList = []
			for dev in valuesDict["listExclDevices"]:
				self.DeviceExcludeList.append(int(dev))

			self.pluginPrefs["listIncStates"] = self.StatesIncludeList
			self.pluginPrefs["listIncDevices"] = self.DeviceIncludeList
			self.pluginPrefs["listExclDevices"] = self.DeviceExcludeList

		self.logger.debug("completed processing closedPrefsConfigUi")

	def BuildConfigurationLists(self):
		self.logger.debug("starting BuildConfigurationLists()")

		self.LastConfigRefresh = datetime.datetime.now()

		for dev in indigo.devices:
			### STATES List
			for kk, vv in self.adaptor.to_json(dev).iteritems():
				try:
					index = -1
					index = [y[0] for y in self.FullStateList].index(kk)
				except:
					index = -1

				if index == -1:
					self.FullStateList.append((kk, 1))
				else:
					count = int(self.FullStateList[index][1]) + 1
					self.FullStateList[index] = (kk, count)

			### Included and Excluded Devices Available to assign list
			if dev.id not in self.DeviceIncludeList and dev.id not in self.DeviceExcludeList:
				self.AvailableIncDevices.append((dev.id, dev.name))
			
			### Excluded Devices Available		
			if dev.id not in self.DeviceIncludeList:
				self.AvailableExlDevices.append((dev.id, dev.name))

		# The States list is a bit different, creating the one that will bind to the GUI
		self.AvailableStatesUI = []

		for ui in sorted(self.FullStateList, key=lambda tup: tup[0]):
			if not ui[0] in self.StatesIncludeList:
				self.AvailableStatesUI.append((ui[0], ui[0] + " (" + str(ui[1]) + ")"))

		# Rebuild the UI lists for Include and Exclude to be ready for use with the config dialog
		self.DeviceIncludeListUI = []

		for item in self.DeviceIncludeList:
			try:
				self.DeviceIncludeListUI.append((item, indigo.devices[int(item)].name))
			except:
				self.logger.error("could not find device " + item + " to add to the InfluxDB device list")
				pass

		self.DeviceListExcludeListUI = []

		for item in self.DeviceExcludeList:
			try:
				self.DeviceListExcludeListUI.append((item, indigo.devices[int(item)].name))
			except:
				self.logger.error("could not find device " + item + " to add to the InfluxDB exclude device list")
				pass

		self.logger.debug("completed BuildConfigurationLists")

	def IncludedStatesListGenerator(self, filter="", valuesDict=None, typeId="", targetId=0):
		self.logger.debug("IncludedStatesListGenerator()")
		self.logger.debug("self.StatesIncludeList: " + str(self.StatesIncludeList))
		toReturn = []

		for item in self.StatesIncludeList:
			toReturn.append((item, item))

		return toReturn

	def IncludedDeviceListGenerator(self, filter="", valuesDict=None, typeId="", targetId=0):
		return self.DeviceIncludeListUI

	def ExcludedDeviceListGenerator(self, filter="", valuesDict=None, typeId="", targetId=0):
		return self.AvailableExlDevices

######
	def AvailableStatesGenerator(self, filter="", valuesDict=None, typeId="", targetId=0):
		return self.AvailableStatesUI

	def AllStatesGenerator(self, filter="", valuesDict=None, typeId="", targetId=0):
		return self.AvailableStatesUI

	def AvailableDevicesGenerator(self, filter="", valuesDict=None, typeId="", targetId=0):
		return self.AvailableIncDevices

######

	def AddDeviceToIncludedDeviceList(self, valuesDict, typeId="", devId=0):
		self.DeviceIncludeList.append(int(valuesDict["menuAvailableDevices"]))
		self.DeviceIncludeListUI.append((valuesDict["menuAvailableDevices"], indigo.devices[int(valuesDict["menuAvailableDevices"])].name))

		for item in self.AvailableIncDevices:
			if item[0] == int(valuesDict["menuAvailableDevices"]):
				self.AvailableIncDevices.remove(item)

		for item in self.AvailableExlDevices:
			if item[0] == int(valuesDict["menuAvailableDevices"]):
				self.AvailableExlDevices.remove(item)

		return valuesDict

	def RemoveDeviceFromIncludedDeviceList(self, valuesDict, typeId="", devId=0):
		for dev in valuesDict["listIncDevices"]:
			indigodev = indigo.devices[int(dev)]

			self.DeviceIncludeList.remove(indigodev.id)
			self.DeviceIncludeListUI.remove((indigodev.id, indigodev.name))

			self.AvailableIncDevices.append((indigodev.id, indigodev.name))
			self.AvailableExlDevices.append((indigodev.id, indigodev.name))

		return valuesDict


	def AddStateToIncludedStateList(self, valuesDict, typeId="", devId=0):
		self.StatesIncludeList.append(valuesDict["menuAvailableStates"])

		for item in self.AvailableStatesUI:
			if item[0] == valuesDict["menuAvailableStates"]:
				self.AvailableStatesUI.remove(item)

		return valuesDict

	def RemoveStateFromIncludedDeviceList(self, valuesDict, typeId=0, devId=0):
		for state in valuesDict["listIncStates"]:
			self.logger.debug("removing: " + str(state))
			self.StatesIncludeList.remove(state)

			for ui in sorted(self.FullStateList, key=lambda tup: tup[0]):
				if ui[0] == state:
					self.AvailableStatesUI.append((ui[0], ui[0] + " (" + str(ui[1]) + ")"))

		return valuesDict

	def ResetStatesIncludedStateList(self, valuesDict, typeId="", devId=0):
		self.logger.debug("resetting to default states")

		self.StatesIncludeList = DEFAULT_STATES[:]

		self.AvailableStatesUI = []		
		for ui in sorted(self.FullStateList, key=lambda tup: tup[0]):
			if not ui[0] in self.StatesIncludeList:
				self.AvailableStatesUI.append((ui[0], ui[0] + " (" + str(ui[1]) + ")"))

		return valuesDict

### Other menu items

	def PrintDeviceToEventLog(self, valuesDict, typeId=0, devId=0):
		dev = indigo.devices[int(valuesDict["menuDevice"])]

		if dev.id in self.DeviceExcludeList:
			indigo.server.log("Device \"" + dev.name + "\" is EXCLUDED and would never be sent to InfluxDB")
			return valuesDict

		if dev.id in self.DeviceIncludeList:
			indigo.server.log("JSON representation (ALL STATES) of device " + dev.name + ":")
			newjson = self.adaptor.diff_to_json(dev, [], False)
		else:
			indigo.server.log("JSON representation (INCLUDED STATES ONLY!) of device " + dev.name + ":")
			newjson = self.adaptor.diff_to_json(dev, self.StatesIncludeList, False)

		if newjson is None:
			indigo.server.log("   the device: \"" + dev.name + "\" is not excluded from updates to InfluxDB, but it contains no states/properties that are cofigured to be sent to Influx/Grafana.")			

		else:
			for kk, vv in newjson.iteritems():
				if not isinstance(vv, indigo.Dict) and not isinstance(vv, dict):
					indigo.server.log("   " + str(kk) + ": " + str(vv))

		return valuesDict

	def PrintStateToEventLog(self, valuesDict, typeId=0, devId=0):
		searchKey = valuesDict["menuProperty"]
		counter = 1

		indigo.server.log("Devices containing the property " + searchKey + ":")
		for dev in indigo.devices:
			### STATES List
			for kk, vv in self.adaptor.to_json(dev).iteritems():
				if not isinstance(vv, indigo.Dict) and not isinstance(vv, dict):
					if kk == searchKey:
						indigo.server.log("   " + str(counter) + ". " + dev.name)
						counter = counter + 1

		return valuesDict