from flask import Flask, request, jsonify
from flask_script import Manager, Server
import os
import sys
import json
import time
import requests
import threading

# restraints
MAX_KEY_LENGTH = 50
SLEEP_TIME = 5
TIME_OUT = 0.5
SPEED = 8

# return codes
INTERNAL_SERVER_ERROR = 500
SERVICE_UNAVAILABLE = 503
BAD_REQUEST = 400
FORBIDDEN = 403
NOT_FOUND = 404
CREATED = 201
OK = 200

#GLOBAL VARS
# keys: non persistent dictionary
# view: empty view containing socket 
# replicasAlive: addresses of running replicas
# vectorClock: vector clock for enforcing causal consistency
keys = {}
viewList = {}
replicasAlive = []
vectorClock = {}
shardGroups = {}
routing = {}

# each replica knows:
#   - its own socket address [SOCKET_ADDRESS]
#   - its copy of the view   [VIEW]
#   - its shard count        [SHARD_COUNT]
socket_address = ""
replica_view = ""
shard_count = 0


# creating app
app = Flask(__name__)
app.debug = True
manager = Manager(app)

# threading variables
dataLock = threading.Lock()
thread = threading.Thread()

# initial put broadcast by the newly added instance
def shareView(retrieveStore):
    requestPath = "/key-value-store-view"
    data = {"socket-address":socket_address}
    broadcastPut(requestPath, data, retrieveStore)

class CustomServer(Server):
    def __call__(self, app, *args, **kwargs):
        app.logger.info("Instance started!")
        sys.stdout.flush()
        shareView(True)
        return Server.__call__(self, app, *args, **kwargs)

# starts thread before 
# the first request recieved
@app.before_first_request
def init():
    thread = threading.Thread(target=validate)
    thread.start()

def validate():
    global viewList
    global replicasAlive
    global thread
    requestPath = "/key-value-store-view"
    
    # does another put broadcast to make sure replicas are up to date

    def check():
        while(True):
            consoleMessages()
            with dataLock:
                shareView(False)
                # print("THREAD CHECK:", vectorClock)
                for addressee in replicasAlive:
                    if addressee == socket_address:
                        continue
                    url = constructURL(addressee, "/broadcast-get")
                    headers = {"content-type": "application/json"}
                    try:
                        response = requests.get(url, headers=headers, timeout=TIME_OUT)
                    except:
                        data = {"socket-address":addressee}
                        if addressee in replicasAlive:
                            app.logger.info(f"Instance not found: {addressee}, deleting now")
                            try:
                                replicasAlive.remove(addressee)
                            except:
                                pass
                            viewList[addressee] = None
                        broadcastDelete(requestPath, data) 
            consoleMessages(SLEEP_TIME)

    thread = threading.Thread(target=check)
    thread.start()

# method for broadcasting put for VIEW operations
def broadcastPut(requestPath, data, retrieveStore):
    global keys
    # loop through all addresses seen
    # to check if previously removed addresses have resurrected
    copy = viewList.copy()
    for addressee in copy:
        response = None
        if addressee == socket_address:
            continue
        url = constructURL(addressee, requestPath)
        headers = {"content-type": "application/json"}
        try:
            response = requests.put(url, data=json.dumps(data), headers=headers, timeout=TIME_OUT)
        except:
            app.logger.info(f"Broadcast from {socket_address} => {addressee} failed!")
            pass
        
        # NOTE: should be logical to populate the kvs using a shard member

        # 1. Add the replica to an existing shard (ID) 
        # 2. grab the store of any online replicas in that store

        # if not keys and retrieveStore:
        #     app.logger.info(f"Attempting to populate Key-Value Store using address: {addressee}")
        #     newKeys =  getStore(addressee)
        #     newClock = getClock(addressee)
        #     if newKeys:
        #         keys.update(newKeys)
        #     if newClock:
        #         vectorClock.update(newClock)

# method for broadcasting delete for VIEW operations
def broadcastDelete(requestPath, data):
    for addressee in replicasAlive:
        # if the addressee is the current replica or
        # if its down, we goto the next replica
        if addressee == socket_address:
            continue
        url = constructURL(addressee, requestPath)
        # url += "/broadcast-delete"
        headers = {"content-type": "application/json"}
        try:
            response = requests.delete(
                url, 
                data=json.dumps(data), 
                headers=headers,
                timeout=TIME_OUT
            )
        except:
            pass

@app.route("/")
def home():
    str = "Testing: "
    if socket_address:
        str += socket_address + " "
    if replica_view:
        str += stringize(replicasAlive)
    str += str(shard_count)
    return str

# replica endpoint for getting viewList
@app.route("/get-view", methods=['GET'])
def replicasSeen():
    returnMsg = viewsMessage(json.dumps(viewList))
    return returnMsg, OK

# endpoints for view operations
@app.route("/key-value-store-view", methods=['GET'])
def getView():
    stringView = stringize(replicasAlive)
    returnMsg = viewMessage(stringView)
    return returnMsg, OK

@app.route("/key-value-store-view", methods=['DELETE'])
def deleteView():
    data = request.get_json()
    addressToDelete = data["socket-address"]
    returnMsg = ""
    returnCode = None

    # if the address is provided and is inside the replicas
    # list of addresses, we delete and update the view
    # NOTE: a replica shouldn't delete itself either of course
    if addressToDelete and addressToDelete in replicasAlive and addressToDelete != socket_address:
        replicasAlive.remove(addressToDelete)
        viewList[addressToDelete] = None
        returnMsg = deleteMessage(True)
        returnCode = OK
    else:
        returnMsg = deleteMessage(False)
        returnCode = NOT_FOUND
    return returnMsg, returnCode

@app.route("/key-value-store-view", methods=['PUT'])
def putView():
    data = request.get_json()
    addressToPut = data["socket-address"]
    if addressToPut not in vectorClock:
        vectorClock[addressToPut] = {0:{}}
    returnMsg = ""
    returnCode = None

    # if the address is not already in the view of the specified replica
    # we add it, else we return the error msg
    if addressToPut and addressToPut not in replicasAlive:
        replicasAlive.append(addressToPut)
        viewList[addressToPut] = "alive"
        returnMsg = putMessage(True)
        returnCode = OK
    else:
        returnMsg = putMessage(False)
        returnCode = NOT_FOUND
    return returnMsg, returnCode

# shard operations
@app.route("/key-value-store-shard/shard-ids", methods=['GET'])
def getShardIDs():
    shardIDs = list(shardGroups.keys())
    returnMsg = shardIDsMessage(json.dumps(shardIDs))
    return returnMsg, OK

@app.route("/key-value-store-shard/node-shard-id", methods=['GET'])
def getShardID():
    shardID = routing.get(socket_address)
    returnMsg = shardIDMessage(shardID)
    return returnMsg, OK

@app.route("/key-value-store-shard/shard-id-members/<id>", methods=['GET'])
def getShardGroup(id):
    shardGroup = shardGroups.get(int(id))
    print("GORUP", shardGroup)
    sys.stdout.flush()
    returnMsg = shardGroupMessage(json.dumps(shardGroup))
    return returnMsg, OK

@app.route("/key-value-store-shard/shard-id-key-count/<id>", methods=['GET'])
def getShardKeyCount(id):
    returnMsg = shardKeyCountMessage(0)
    return returnMsg, OK

# helper functions for constructing view json msgs
def viewMessage(view):
    retmsg = jsonify({
        "message":"View retrieved successfully",
        "view":view
    })
    return trimMsg(retmsg)

def viewsMessage(views):
    retmsg = jsonify({
        "message":"All views retrieved successfully",
        "views":views
    })
    return trimMsg(retmsg)

def deleteMessage(success):
    retmsg = ""
    if success:
        retmsg = jsonify({
            "message":"Replica deleted successfully from the view"
        })
    else:
        retmsg = jsonify({
            "error":"Socket address does not exist in the view",
            "message":"Error in DELETE"
        })
    return trimMsg(retmsg)

def putMessage(success):
    retmsg = ""
    if success:
        retmsg = jsonify({
            "message":"Replica added successfully to the view"
        })
    else:
        retmsg = jsonify({
            "error":"Socket address already exists in the view",
            "message":"Error in PUT"
        })
    return trimMsg(retmsg)

def shardIDsMessage(ids):
    retmsg = ""
    retmsg = jsonify({
        "message":"Shard IDs retrieved successfully",
        "shard-ids":json.loads(ids)
    })
    return trimMsg(retmsg)

def shardIDMessage(id):
    retmsg = ""
    retmsg = jsonify({
        "message":"Shard ID of the node retrieved successfully",
        "shard-id":id
    })
    return trimMsg(retmsg)

def shardGroupMessage(group):
    retmsg = ""
    retmsg = jsonify({
        "message":"Members of shard ID retrieved successfully", 
        "shard-id-members":json.loads(group)
    })
    return trimMsg(retmsg)

def shardKeyCountMessage(keys):
    retmsg = ""
    retmsg = jsonify({
        "message":"Key count of shard ID retrieved successfully",
        "shard-id-key-count":keys
    })
    return trimMsg(retmsg)

# NOTE: this only takes in flask.wrappers.Response objects
# method for removing new line character from jsonify
def trimMsg(retmsg):
    datastring = retmsg.get_data(as_text=True)[:-1]
    retmsg.set_data(datastring)
    return retmsg

# method for constructing URLs for sending
def constructURL(address, requestPath):
    return "http://" + address + requestPath

# view helper functions
def parseList(data):
    dataList = []
    if data:
        dataList = data.split(',')
    return dataList

# to convert the view to a string
def stringize(dataList):
    dataString = ",".join(dataList)
    return dataString

# messages for console
def consoleMessages(sleep=None):
    clocks = ["◰", "◳", "◲", "◱"]
    verify = ["|", "/", "-", "\\"]
    cri = ["｡ ･   ･ (>▂<) ･    ･ ｡", ". 。･ 。 (>▂<) 。･ 。."]
    slep = 5 * ["(︶.︶✽)"] + 2 * ["(︶｡︶✽)"] + 3 * ["(︶o︶✽)"] +  2 * ["(︶｡︶✽)"]
    bar = ["[","[=","[==","[===","[====","[=====","[======","[=======","[========","[=========","[==========","[=========","[========","[=======","[======","[=====","[====","[===","[==","[="]
    bar2 = ["==========]","=========]","========]","=======]","======]","=====]","====]","===]","==]","=]","]","=]","==]","===]","====]","=====]","======]","=======]","========]","=========]"]
    i = 0
    if sleep is not None:
        clock = 0
        for i in range(sleep*SPEED):
            if i % SPEED == 0:
                clock+=1
            print(clocks[clock%len(clocks)] + bar[i % len(bar)] + slep[i%len(slep)] + bar2[i % len(bar2)] + clocks[clock%len(clocks)], end="\r")
            sys.stdout.flush()
            time.sleep(1/SPEED)
        return
    while True:
        if len([x for x in viewList.values() if x]) > 1:
            clock = 0
            for i in range(35):
                print("→ Verifying replicas " + verify[i % len(verify)], end="\r")
                sys.stdout.flush()
                time.sleep(.04)
            return
        else:
            for i in range(2*SPEED):
                print(bar[i % len(bar)] + "No other replicas up!" + bar2[i % len(bar2)], end="\r")
                sys.stdout.flush()
                time.sleep(1/SPEED)
            print('\n')
            sys.stdout.flush()
            return

if __name__ == "__main__":
    # populating environmental variables
    try:
        socket_address = os.environ['SOCKET_ADDRESS']
        replica_view = os.environ['VIEW']
        shard_count = int(os.environ['SHARD_COUNT'])
    except:
        pass

    # creating the viewList for later manipulation
    replicasAlive = parseList(replica_view)

    nodesPerShard = len(replicasAlive) // shard_count

    for replica in replicasAlive:
        viewList[replica] = "alive"
        vectorClock[replica] = {0:{}}

    for i, replica in enumerate(replicasAlive):
        shardID = i%shard_count
        if shardID in shardGroups:
            shardGroups[shardID].append(replica)
        else:
            shardGroups[shardID] = [replica] 
        routing[replica] = shardID

    # add command for docker to run the custom server
    manager.add_command('run', CustomServer(host='0.0.0.0', port=8085))
    manager.run()