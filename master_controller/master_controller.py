# -*- coding: utf-8 -*-
#!/usr/bin/env python

# HAPI Master Controller v1.0
# Author: Tyler Reed
# Release: June 2016 Alpha
#*********************************************************************
#Copyright 2016 Maya Culpa, LLC
#
#This program is free software: you can redistribute it and/or modify
#it under the terms of the GNU General Public License as published by
#the Free Software Foundation, either version 3 of the License, or
#(at your option) any later version.
#
#This program is distributed in the hope that it will be useful,
#but WITHOUT ANY WARRANTY; without even the implied warranty of
#MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#GNU General Public License for more details.
#
#You should have received a copy of the GNU General Public License
#along with this program.  If not, see <http://www.gnu.org/licenses/>.
#*********************************************************************

import sqlite3
import sys
import operator
import time
import schedule
import datetime
import urllib2
import json
import subprocess
import rtu_comm
import os
from multiprocessing import Process, Pipe
import gevent, gevent.server
from telnetsrv.green import TelnetHandler, command
import logging

rtus = []
reload(sys)
sys.setdefaultencoding('UTF-8')
version = "1.0"

class RemoteTerminalUnit(object):
    def __init__(self):
        self.rtuid = ""
        self.protocol = ""
        self.address = ""
        self.version = ""
        self.online = 0
        self.pin_modes = {}

class Site(object):
    """docstring for Site"""
    def __init__(self):
        self.site_id = ""
        self.name = ""
        self.wunder_key = ""
        self.operator = ""
        self.email = ""
        self.phone = ""
        self.location = ""
        self.net_iface = ""
        self.rtus = []
        self.logger = None


    def load_site_data(self):
        try:
            conn = sqlite3.connect('hapi.db')
            c=conn.cursor()
            db_elements = c.execute("SELECT site_id, name, wunder_key, operator, email, phone, location, net_iface FROM site LIMIT 1;")
            for field in db_elements:
                self.site_id = field[0]
                self.name = field[1]
                self.wunder_key = field[2]
                self.operator = field[3]
                self.email = field[4]
                self.phone = field[5]
                self.location = field[6]
                self.net_iface = field[7]
            conn.close()
        except Exception, excpt:
            if self.logger != None:
                self.logger.exception("Error loading site data: %s", excpt)


    def discover_rtus(self):
        print "Discovering RTUs..."
        valid_ip_addresses = self.scan_for_rtus()
        self.rtus = []

        for ip_address in valid_ip_addresses:
            print "Connecting to RTU at", ip_address
            try:
                target_rtu = rtu_comm.RTUCommunicator()
                response = target_rtu.send_to_rtu(ip_address, 80, 5, "sta").split('\r\n')
                print response[0], "found at", ip_address, "running", response[1]
                rtu = RemoteTerminalUnit()
                rtu.rtuid = response[0]
                rtu.address = ip_address
                rtu.version = response[1]
                rtu.online = True
                get_pin_modes(rtu)
                self.rtus.append(rtu)
            except Exception, excpt:
                if self.logger != None:
                    self.logger.exception("Error communicating with rtu at " + ip_address + ": %s", excpt)

        return self.rtus

    def scan_for_rtus(self):
        rtu_addresses = []
        try:
            print "Scanning local network for RTUs..."
            el1 = "arp-scan".encode("ascii")
            el2 = str("--interface=" + self.net_iface).encode("ascii")
            el3 = "--localnet".encode("ascii")
            #netscan = subprocess.check_output(["arp-scan", "--interface=" + self.net_iface, "--localnet"])
            netscan = subprocess.check_output([el1, el2, el3])
            netscan = netscan.split('\n')
            for machine in netscan:
                if machine.find("de:ad:be:ef") > -1:
                    els = machine.split("\t")
                    ip_address = els[0]
                    print "Found RTU at: ", ip_address
                    rtu_addresses.append(ip_address)

        except Exception, excpt:
            if self.logger != None:
                self.logger.exception("Error scanning local network: %s", excpt)

        return rtu_addresses


class Scheduler(object):
    
    def __init__(self):
        self.running = True
        self.logger = None
        self.site = None

    def load_interval_schedule(self):
        job_list = []
        try:
            conn = sqlite3.connect('hapi.db')
            c=conn.cursor()

            db_jobs = c.execute("SELECT job_id, job_name, rtuid, command, time_unit, interval, at_time, enabled FROM interval_schedule;")
            for row in db_jobs:
                job = IntervalJob()
                job.job_id = row[0]
                job.job_name = row[1]
                job.rtuid = row[2]
                job.command = row[3].encode("ascii")
                job.time_unit = row[4]
                job.interval = row[5]
                job.at_time = row[6]
                job.enabled = row[7]
                job_list.append(job)

            conn.close()
        except Exception, excpt:
            print "Error loading interval_schedule. %s", excpt

        return job_list

    def prepare_jobs(self, jobs):
        for job in jobs:
            if job.time_unit.lower() == "month":
                if job.interval > -1:
                    schedule.every(job.interval).months.do(self.run_job, job)
                    print "Loading monthly job:", job.job_name
            elif job.time_unit.lower() == "week":
                if job.interval > -1:
                    schedule.every(job.interval).weeks.do(self.run_job, job)
                    print "Loading weekly job:", job.job_name
            elif job.time_unit.lower() == "day":
                if job.interval > -1:
                    schedule.every(job.interval).days.do(self.run_job, job)
                    print "Loading daily job:", job.job_name
                else:
                    schedule.every().day.at(job.at_time).do(self.run_job, job)
                    print "Loading daily job:", job.job_name
            elif job.time_unit.lower() == "hour":
                if job.interval > -1:
                    schedule.every(job.interval).hours.do(self.run_job, job)
                    print "Loading hourly job:", job.job_name
            elif job.time_unit.lower() == "minute":
                if job.interval > -1:
                    schedule.every(job.interval).minutes.do(self.run_job, job)
                    print "Loading minute job:", job.job_name
                else:
                    schedule.every().minute.do(self.run_job, job)
                    print "Loading minute job:", job.job_name

    def run_job(self, job):
        if self.running == True:
            print 'Running', job.command, "on", job.rtuid
            command = ""
            response = ""
            job_rtu = None

            if job.rtuid.lower() == "virtual":
                try:
                    response = eval(job.command)
                    log_sensor_data(response, True, self.logger)
                except Exception, excpt:
                    error = "Error running job " + job.job_name + " on " + job_rtu.rtuid + ": " + excpt
                    print error
                    if self.logger != None:
                        self.logger.exception(error)
            else:
                try:
                    for rtu_el in self.site.rtus:
                        if rtu_el.rtuid == job.rtuid:
                            if rtu_el.online == 1:
                                job_rtu = rtu_el

                    if (job_rtu != None):
                        command = job.command
                        target_rtu = rtu_comm.RTUCommunicator()
                        response = target_rtu.send_to_rtu(job_rtu.address, 80, 5, command)

                        if (job.job_name == "Log Data"):
                            log_sensor_data(response, False, self.logger)
                        elif (job.job_name == "Log Status"):
                            pass
                        else:
                            log_command(job)
                    else:
                        print "Could not find rtu."
                        if self.logger != None:
                            self.logger.info("Could not find rtu." + job.rtuid)

                except Exception, excpt:
                    error = "Error running job " + job.job_name + " on " + job_rtu.rtuid + ": " + excpt
                    print error
                    if self.logger != None:
                        self.logger.exception(error)

class HAPIListener(TelnetHandler):
    global the_rtu
    global the_rtus

    the_rtu = None
    the_rtus = []

    site = Site()
    site.load_site_data()

    if site != None:
        WELCOME = "\n" + "Welcome to HAPI facility " + site.name + '\n'
        WELCOME = WELCOME + "Operator: " + site.operator + '\n'
        WELCOME = WELCOME + "Phone: " + site.phone + '\n'
        WELCOME = WELCOME + "Email: " + site.email + '\n'
        WELCOME = WELCOME + "Location: " + site.location + '\n'
        WELCOME = WELCOME + "\n" + 'Type "help" for a list of valid commands.' + '\n'
    else:
        WELCOME = "No site data found."

    PROMPT = "HAPI> "

    @command('cmd')
    def command_cmd(self, params):
        '''<command to be run on connected RTU>
        Sends a command to the connected RTU

        '''
        if the_rtu == None:
            self.writeresponse("You are not connected to an RTU.")
        else:
            command = params[0]

            self.writeresponse("Executing " + command + " on " + the_rtu.rtuid + "...")
            target_rtu = rtu_comm.RTUCommunicator()
            response = target_rtu.send_to_rtu(the_rtu.address, 80, 5, command)
            self.writeresponse(response)
            job = IntervalJob()
            job.job_name = command
            job.rtuid = the_rtu.rtuid
            log_command(job)

    @command('connect')
    def command_connect(self, params):
        '''<Name of RTU>
        Connects to the specified RTU

        '''
        global the_rtu
        rtu_name = params[0]
        the_rtu = None

        for rtu in the_rtus:
            if rtu.rtuid.lower() == rtu_name.lower():
                the_rtu = rtu

        if the_rtu != None:
            self.writeresponse("Connecting to " + rtu_name + "...")
            target_rtu = rtu_comm.RTUCommunicator()
            response = target_rtu.send_to_rtu(the_rtu.address, 80, 5, "env")            
            self.writeresponse(response)
            PROMPT = the_rtu.rtuid + "> "

        else:
            self.writeresponse(rtu_name + " is not online at this site.")

    @command('continue')
    def command_continue(self, params):
        '''
        Starts the Master Controller's Scheduler

        '''
        f = open("ipc.txt", "wb")
        f.write("run")
        f.close() 

    @command('pause')
    def command_pause(self, params):
        '''
        Pauses the Master Controller's Scheduler

        '''
        f = open("ipc.txt", "wb")
        f.write("pause")
        f.close()

    @command('rtus')
    def command_rtus(self, params):
        '''
        List all RTUs discovered at this site.

        '''
        global the_rtus
        self.writeresponse("Discovering RTUs...")
        site = Site()
        site.load_site_data()
        the_rtus = site.discover_rtus()
        for rtu in the_rtus:
            self.writeresponse(rtu.rtuid + " found at " + rtu.address + " is running HAPI " + rtu.version + ".")
    
    @command('status')
    def command_status(self, params):
        '''
        Return operational status of the Master Controller

        '''
        data = '\n### Master Controller Status ###\n'
        data = data + '  Version v' + version + '\n'
        data = data + '  Copyright 2016, Maya Culpa, LLC\n'
        data = data + '  Platform: ' + sys.platform + '\n'
        data = data + '  Encoding: ' + sys.getdefaultencoding() + '\n'
        data = data + '  Python Information\n'
        data = data + '     Executable: ' + sys.executable + '\n'
        data = data + '     Version: ' + sys.version.replace('\n', " ") + '\n'
        data = data + '     location: ' + sys.executable + '\n'
        data = data + '  Timestamp: ' + str(datetime.datetime.now()) + '\n'
        data = data + '################################\n'
        self.writeresponse(data)

    @command('stop')
    def command_stop(self, params):
        '''
        Kills the HAPI listener service

        '''
        f = open("ipc.txt", "wb")
        f.write("stop")
        f.close()


def get_sensor_data():
    def dict_factory(cursor, row):
        d = {}
        for idx, col in enumerate(cursor.description):
            d[col[0]] = row[idx]
        return d
     
    connection = sqlite3.connect("hapi.db")
    connection.row_factory = dict_factory
     
    cursor = connection.cursor()
     
    cursor.execute("select a.rtuid, a.name, s.timestamp, s.value, s.unit from assets a INNER JOIN sensor_data s on a.asset_id = s.asset_id ORDER by s.timestamp")
     
    # fetch all or one we'll go for all.
    results = cursor.fetchall()
    f = open("sensor_data.json", "wb")
    f.write(json.dumps(results))
    f.close()
     
    connection.close()

def get_weather():
    response = ""
    try:
        response = ""
        command = 'http://api.wunderground.com/api/' + site.wunder_key + '/geolookup/conditions/q/OH/Columbus.json'
        print command
        f = urllib2.urlopen(command)
        json_string = f.read()
        parsed_json = json.loads(json_string)
        response = parsed_json['current_observation']
        f.close()
    except Exception, excpt:
        print "Error getting weather data.", excpt
    return response

def get_image():
    command = "fswebcam -p YUYV -d /dev/video0 -r 1280x720 image.jpg"
    # ex: to store a image in to db
    # public void insertImg(int id , Bitmap img ) {   
    #     byte[] data = getBitmapAsByteArray(img); // this is a function
    #     insertStatement_logo.bindLong(1, id);       
    #     insertStatement_logo.bindBlob(2, data);
    #     insertStatement_logo.executeInsert();
    #     insertStatement_logo.clearBindings() ;
    # }

    #  public static byte[] getBitmapAsByteArray(Bitmap bitmap) {
    #     ByteArrayOutputStream outputStream = new ByteArrayOutputStream();
    #     bitmap.compress(CompressFormat.PNG, 0, outputStream);       
    #     return outputStream.toByteArray();
    # }

    # to retrieve a image from db
    # public Bitmap getImage(int i){
    #     String qu = "select img  from table where feedid=" + i ;
    #     Cursor cur = db.rawQuery(qu, null);
    #     if (cur.moveToFirst()){
    #         byte[] imgByte = cur.getBlob(0);
    #         cur.close();
    #         return BitmapFactory.decodeByteArray(imgByte, 0, imgByte.length);
    #     }
    #     if (cur != null && !cur.isClosed()) {
    #         cur.close();
    #     }       
    #     return null ;
    # } 
    response = parsed_json['current_observation']
    f.close()
    return response

class IntervalJob(object):

    def __init__(self):
        self.job_id = -1
        self.job_name = ""
        self.rtuid = ""
        self.command = ""
        self.time_unit = ""
        self.interval = -1
        self.at_time = ""
        self.enabled = 0

class PinMode(object):
    def __init__(self):
        self.pin = ""
        self.mode = 0
        self.default_value = 0
        self.pos = 0

class Asset(object):
    def __init__(self):
        self.asset_id = -1
        self.rtuid = ""
        self.abbreviation = ""
        self.name = ""
        self.pin = ""
        self.unit = ""

def push_log_data(sensor_name):
    log = RawLog()
    log.read_raw_log()
    for entry in log.log_entries:
        data = json.loads(entry.data)
        print data.rtuid, data.timestamp, sensor_name, data[sensor_name]

def get_pin_modes(rtu):
    try:
        conn = sqlite3.connect('hapi.db')
        c=conn.cursor()

        sql = "SELECT p.pin, p.mode, p.def_value, p.pos FROM pins p WHERE p.rtuid = \'" + rtu.rtuid + "\' ORDER BY p.pos;"
        db_elements = c.execute(sql)
        for unit in db_elements:
            pin_mode = PinMode()
            pin_mode.pin = unit[0]
            pin_mode.mode = unit[1]
            pin_mode.default_value = unit[2]
            pin_mode.pos = unit[3]
            rtu.pin_modes.update({pin_mode.pin : pin_mode})
        conn.close()
    except Exception, excpt:
        print "Error loading pin mode table. %s", excpt

    return

def get_assets():
    assets = []
    try:
        conn = sqlite3.connect('hapi.db')
        c=conn.cursor()
        sql = "SELECT asset_id, rtuid, abbreviation, name, pin, unit FROM assets;"
        rows = c.execute(sql)
        for field in rows:
            asset = Asset()
            asset.asset_id = field[0]
            asset.rtuid = field[1]
            asset.abbreviation = field[2]
            asset.name = field[3]
            asset.pin = field[4]
            asset.unit = field[5]
            assets.append(asset)
        conn.close()
    except Exception, excpt:
        print "Error loading asset table. %s", excpt

    return assets

def validate_pin_modes(online_rtus):
    print "Validating pin modes..."
    problem_rtus = []

    # Check pin mode settings
    for rtu in online_rtus:
        target_rtu = rtu_comm.RTUCommunicator()
        pmode_from_rtu = target_rtu.send_to_rtu(rtu.address, 80, 5, "gpm")

        pmode_from_db = ""
        for db_pin_mode in sorted(rtu.pin_modes.values(), key=operator.attrgetter('pos')):
            pmode_from_db += db_pin_mode.pin
            pmode_from_db += str(db_pin_mode.mode)

        pin_mode_ok = True
        for i in range(0, len(pmode_from_rtu) - 2):
            if pmode_from_rtu[i] != pmode_from_db[i]:
                pin_mode_ok = False
        if pin_mode_ok == False:
            print "RTU", rtu.rtuid, "has an incongruent pin mode."
            print "RTU pins", pmode_from_rtu
            print "DB pins", pmode_from_db
            problem_rtus.append(rtu)
        else:
            print "Pin mode congruence verified between", rtu.rtuid, "and the database."
    return problem_rtus

def log_command(job):

    timestamp = '"' + str(datetime.datetime.now()) + '"'
    name = '"' + job.job_name + '"'
    rtuid = '"' + job.rtuid + '"'
    command = "INSERT INTO command_log (rtuid, timestamp, command) VALUES (" + rtuid + ", " + timestamp + ", " + name + ")"
    print command
    conn = sqlite3.connect('hapi.db')
    c=conn.cursor()
    c.execute(command)
    conn.commit()
    conn.close()

def log_sensor_data(data, virtual, logger):
    assets = get_assets()
    if virtual == False:
        try:
            for asset in assets:
                parsed_json = json.loads(data)
                if asset.rtuid == parsed_json['name']:
                    value = parsed_json[asset.pin]
                    timestamp = '"' + str(datetime.datetime.now()) + '"'
                    unit = '"' + asset.unit + '"'
                    command = "INSERT INTO sensor_data (asset_id, timestamp, value, unit) VALUES (" + str(asset.asset_id) + ", " + timestamp + ", " + value + ", " + unit + ")"
                    print command
                    conn = sqlite3.connect('hapi.db')
                    c=conn.cursor()
                    c.execute(command)
                    conn.commit()
                    conn.close()
        except Exception, excpt:
            print "Error logging sensor data.", excpt
    else:
        # For virtual assets, assume that the data is already parsed JSON
        try:
            for asset in assets:
                if asset.rtuid == "virtual":
                    if asset.abbreviation == "weather":
                        value = float(str(data[asset.pin]).replace("%", ""))
                        timestamp = '"' + str(datetime.datetime.now()) + '"'
                        unit = '"' + asset.unit + '"'
                        command = "INSERT INTO sensor_data (asset_id, timestamp, value, unit) VALUES (" + str(asset.asset_id) + ", " + timestamp + ", " + str(value) + ", " + unit + ")"
                        print command
                        conn = sqlite3.connect('hapi.db')
                        c=conn.cursor()
                        c.execute(command)
                        conn.commit()
                        conn.close()
        except Exception, excpt:
            error = "Error logging sensor data: " + excpt
            print error
            if logger != None:
                logger.exception(error)


    #location = parsed_json['location']['city']
    #temp_f = parsed_json['current_observation']['temp_f']
    #temp_c = parsed_json['current_observation']['temp_c']
    #rel_hmd = parsed_json['current_observation']['relative_humidity']
    #pressure = parsed_json['current_observation']['pressure_mb']
    #print "Current weather in %s" % (location)
    #print "    Temperature is: %sF, %sC" % (temp_f, temp_c)
    #print "    Relative Humidity is: %s" % (rel_hmd)
    #print "    Atmospheric Pressure is: %smb" % (pressure)
    #response = parsed_json['current_observation']

def run_listener(conn):
    server = gevent.server.StreamServer(("", 8023), HAPIListener.streamserver_handle)
    server.serve_forever()
    
def main(argv):
    global rtus
    global site

    logger_level = logging.DEBUG
    logger = logging.getLogger('hapi_master_controller')
    logger.setLevel(logger_level)

    # create logging file handler
    file_handler = logging.FileHandler('hapi_mc.log', 'w')
    file_handler.setLevel(logger_level)

    # create logging console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logger_level)

    #Set logging format
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    try:
        site = Site()
        site.logger = logger
        site.load_site_data()
        rtus = site.discover_rtus()
        problem_rtus = validate_pin_modes(rtus)
    except Exception, excpt:
        logger.exception("Error loading site information. %s", excpt)

    if site != None:
        for rtu in problem_rtus:
            print "RTU", rtu.rtuid, "has pin modes incongruent with the database."

        if len(site.rtus) == 0:
            print "There are no RTUs online."
        elif len(site.rtus) == 1:
            print "There is 1 RTU online."
        else:
            print "There are", len(site.rtus), "online."


    try:
        print "Initializing HAPI Listener..."
        listener_parent_conn, listener_child_conn = Pipe()
        p = Process(target=run_listener, args=(listener_child_conn,))
        p.start()
        print "HAPI Listener is online."
    except Exception, excpt:
        logger.exception("Error loading initializing listener. %s", excpt)

    # Loading scheduled jobs
    try:
        print "Initializing scheduler..."
        scheduler = Scheduler()
        scheduler.site = site
        scheduler.logger = logger
        scheduler.prepare_jobs(scheduler.load_interval_schedule())
        count = 1
        print "Scheduler is initialized and running."
    except Exception, excpt:
        logger.exception("Error initializing scheduler. %s", excpt)

    while 1:
        #print listener_parent_conn.recv()
        try:
            if count % 60 == 0:
                print ".",
            time.sleep(5)
            count = count + 5
            schedule.run_pending()

            if os.path.isfile("ipc.txt"):
                f = open("ipc.txt", "rb")
                data = f.read()
                f.close()
                open("ipc.txt", 'w').close()
                if data != "":
                    if data == "run":
                        scheduler.running = True
                        print "The scheduler is running."
                    elif data == "pause":
                        print "The scheduler has been paused."
                        scheduler.running = False
                    else:
                        print "Received from Listener: " + data
        except Exception, excpt:
            logger.exception("Error in Master Controller main loop. %s", excpt)            
            

if __name__ == "__main__":
    main(sys.argv[1:])
