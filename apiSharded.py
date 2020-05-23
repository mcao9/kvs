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

app = Flask(__name__)
app.debug = True
manager = Manager(app)

#GLOBAL VARS
# keys: non persistent dictionary
# view: empty view containing socket 
# addresses of running replicas
# vector clock for enforcing
# causal consistency
keys = {}
viewList = {}
replicasAlive = []
vectorClock = {}

# each replica knows:
#   - its own socket address [SOCKET_ADDRESS]
#   - its copy of the view   [VIEW]
address = ""
view = ""

# thread init and lock
dataLock = threading.Lock()
thread = threading.Thread()

# 1. for the first request
# this function is initialized, creating a background thread that
# checks for offline replicas

# 2. all other requests
# this function runs forever in the background (every 10 seconds)
# change SLEEP_TIME for setting to a desired interval

# initial put broadcast by the newly added instance
def shareView(retrieveStore):
    requestPath = "/key-value-store-view"
    data = {"socket-address":address}
    broadcastPut(requestPath, data, retrieveStore)

# custom server to run the shareView() function right from the get-go
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
                    if addressee == address:
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

# for debugging purposes
@app.route("/")
def home():
    str = "Testing: "
    if address:
        str += address + " "
    if view:
        str += stringize(replicasAlive)
    return str

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
    if addressToDelete and addressToDelete in replicasAlive and addressToDelete != address:
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

# endpoint for requests used to check dead replicas
@app.route("/broadcast-get", methods=['GET'])
def getCheck():
    returnMsg = jsonify({
        "message":"Replica Alive!"
    })
    return trimMsg(returnMsg), OK

# key-value store endpoints (copied over from assignment 2)
@app.route("/key-value-store/<key>", methods=['GET'])
def getKey(key):
    if key in keys and keys[key] != None:
        returnMsg = existsKeyMessage(json.dumps(vectorClock), "Retrieved successfully", keys[key])
        return returnMsg, OK
    else:
        returnMsg = badKeyRequest(False, "Key does not exist", "Error in GET")
        return returnMsg, NOT_FOUND

@app.route("/key-value-store/<key>", methods=['DELETE'])
def deleteKey(key):
    global vectorClock
    data = request.get_json()
    metaDataString = data['causal-metadata']

    if metaDataString == "":
        app.logger.info("------FIRST DELETE ERROR------")
    else:
        metaDataString = metaDataString.replace("'", "\"")
        receivedVectorClock = json.loads(metaDataString)
        receivedVectorClock = {k: {int(innerKey):v for innerKey, v in receivedVectorClock[k].items()} for k,v in receivedVectorClock.items() }

        biggerClock, concurrent = compareClocks(vectorClock, receivedVectorClock)

        if vectorClock == biggerClock:
            pass

        elif receivedVectorClock == biggerClock:
            for socket in receivedVectorClock.keys():
                upToDate = max(receivedVectorClock[socket])
                kvPair = receivedVectorClock[socket][upToDate]
                keys.update(kvPair)

            vectorClock.update(receivedVectorClock)

        elif concurrent:
            for socket in receivedVectorClock.keys():
                upToDate = max(receivedVectorClock[socket])
                localHigh = max(vectorClock[socket])
                if upToDate > localHigh:
                    kvPair = receivedVectorClock[socket][upToDate]
                    keys.update(kvPair)
                    vectorClock[socket] = receivedVectorClock.get(socket)

    if key in keys and keys[key] != None:
        keys[key] = None

        # print("INCREMENTING CLOCK DUE TO DELETION")
        versions = vectorClock.get(address)
        merged = {**vectorClock[address][max(versions)], **{key:None}}
        vectorClock[address].update({(max(versions) + 1): merged })

        metaDataString = json.dumps(vectorClock)

        requestPath = "/delete-key-broadcast/" + key
        with dataLock:
            threading.Thread(target = broadcastDeleteKey, args=(requestPath, metaDataString,)).start()

        returnMsg = putKeyMessage("Deleted successfully", metaDataString)
        return returnMsg, OK
    else:
        returnMsg = badKeyRequest(False, "Key does not exist", "Error in DELETE")
        return returnMsg, NOT_FOUND

@app.route("/key-value-store/<key>", methods=['PUT'])
def putKey(key):
    global vectorClock

    newKey = False
    data = request.get_json()

    value = data['value']
    metaDataString = data['causal-metadata']

    if not value:
        returnMsg = badKeyRequest("", "Value is missing", "Error in PUT")
        return returnMsg, BAD_REQUEST

    if metaDataString == "":
        app.logger.info("------FIRST PUT------")
    else:
        metaDataString = metaDataString.replace("'", "\"")
        receivedVectorClock = json.loads(metaDataString)
        receivedVectorClock = {k: {int(innerKey):v for innerKey, v in receivedVectorClock[k].items()} for k,v in receivedVectorClock.items() }

        biggerClock, concurrent = compareClocks(vectorClock, receivedVectorClock)
        # print("COMPARISON------------", vectorClock, receivedVectorClock)

        # if the local clock is strictly bigger
        if vectorClock == biggerClock:
            pass
        # if the recieved clock is stricly bigger
        elif receivedVectorClock == biggerClock:
            # print("CLOCK RETRIEVED LARGER!!!")

            for socket in receivedVectorClock.keys():
                upToDate = max(receivedVectorClock[socket])
                kvPair = receivedVectorClock[socket][upToDate]
                print(kvPair)
                sys.stdout.flush()
                keys.update(kvPair)

            vectorClock.update(receivedVectorClock)
        
        elif concurrent:
            # print("CONCURRENCY DETECTED")
            for socket in receivedVectorClock.keys():
                upToDate = max(receivedVectorClock[socket])
                localHigh = max(vectorClock[socket])
                if upToDate > localHigh:
                    kvPair = receivedVectorClock[socket][upToDate]
                    keys.update(kvPair)
                    vectorClock[socket] = receivedVectorClock.get(socket)

    # its a new key if it doesn't exist in the store
    # None means that it was deleted previously, so we check that too
    if key not in keys or keys[key] == None:
        newKey = True

    # add the key
    keys[key] = value

    # increment the vector clock and update causal-metadata
    # print("INCREMENTING CLOCK DUE TO PUT")
    versions = vectorClock.get(address)
    merged = {**vectorClock[address][max(versions)], **{key:value}}
    vectorClock[address].update({(max(versions) + 1): merged })
    # print("RESULTING VC AFTER:", vectorClock)
    metaDataString = json.dumps(vectorClock)

    # create a thread to broadcast the PUT operation
    requestPath = "/key-broadcast/" + key
    with dataLock:
        threading.Thread(target = broadcastPutKey, args=(requestPath, metaDataString, value,)).start()

    # and output the appropriate return message
    if newKey:
        returnMsg = putKeyMessage("Added successfully", metaDataString)
        return returnMsg, CREATED
    else:
        returnMsg = putKeyMessage("Updated successfully", metaDataString)
        return returnMsg, OK

# receiving endpoint for a key broadcast
@app.route("/key-broadcast/<key>", methods=['PUT'])
def putKeyBroadcast(key):
    # since we checked the validity of the data in the initial 
    # put message prior to the broadcast, we don't here
    global vectorClock
    data = request.get_json()

    value = data['value']
    metaDataString = data['causal-metadata']

    if not value:
        returnMsg = badKeyRequest("", "Value is missing", "Error in PUT")
        return returnMsg, BAD_REQUEST


    if metaDataString != "":
        metaDataString = metaDataString.replace("'", "\"")
        receivedVectorClock = json.loads(metaDataString)
        receivedVectorClock = {k: {int(innerKey):v for innerKey, v in receivedVectorClock[k].items()} for k,v in receivedVectorClock.items() }

        # print(receivedVectorClock)

        biggerClock, concurrent = compareClocks(vectorClock, receivedVectorClock)

        # if the local clock is strictly bigger which should NEVER happen since its a broadcast
        if vectorClock == biggerClock:
            pass
        # if the recieved clock is stricly bigger
        elif receivedVectorClock == biggerClock:
            # print("CAUSAL DESCEPTANCY")

            for socket in receivedVectorClock.keys():
                upToDate = max(receivedVectorClock[socket])
                kvPair = receivedVectorClock[socket][upToDate]
                keys.update(kvPair)

            vectorClock.update(receivedVectorClock)
            
        elif concurrent:
            # print("CONCURRENCY DETECTED")

            for socket in receivedVectorClock.keys():
                upToDate = max(receivedVectorClock[socket])
                localHigh = max(vectorClock[socket])
                if upToDate > localHigh:
                    kvPair = receivedVectorClock[socket][upToDate]
                    keys.update(kvPair)
                    vectorClock[socket] = receivedVectorClock.get(socket)


    # we don't update the clock since the one received should have already incremented

    metaDataString = json.dumps(vectorClock)
    
    if key not in keys or keys[key] == None:
        # add the key to the dictionary if non-existent
        keys[key] = value
        returnMsg = putKeyMessage("Added successfully", metaDataString)
        return returnMsg, CREATED
    else:
        # else update the existing key's value
        keys[key] = value
        returnMsg = putKeyMessage("Updated successfully", metaDataString)
        return returnMsg, OK

@app.route("/delete-key-broadcast/<key>", methods=['DELETE'])
def deleteKeyBroadcast(key):
    global vectorClock
    data = request.get_json()

    metaDataString = data['causal-metadata']

    if metaDataString != "":
        metaDataString = metaDataString.replace("'", "\"")
        receivedVectorClock = json.loads(metaDataString)
        receivedVectorClock = {k: {int(innerKey):v for innerKey, v in receivedVectorClock[k].items()} for k,v in receivedVectorClock.items() }

        biggerClock, concurrent = compareClocks(vectorClock, receivedVectorClock)

        if vectorClock == biggerClock:
            pass

        elif receivedVectorClock == biggerClock:
            # print("CAUSAL DESCEPTANCY")

            for socket in receivedVectorClock.keys():
                upToDate = max(receivedVectorClock[socket])
                kvPair = receivedVectorClock[socket][upToDate]
                keys.update(kvPair)

            vectorClock.update(receivedVectorClock)

        elif concurrent:
            # print("CONCURRENCY DETECTED")

            for socket in receivedVectorClock.keys():
                upToDate = max(receivedVectorClock[socket])
                localHigh = max(vectorClock[socket])
                if upToDate > localHigh:
                    kvPair = receivedVectorClock[socket][upToDate]
                    keys.update(kvPair)
                    vectorClock[socket] = receivedVectorClock.get(socket)

        
    metaDataString = json.dumps(vectorClock)

    if key in keys or keys[key] != None:
        keys[key] = None
        returnMsg = putKeyMessage("Deleted successfully", metaDataString)
        return returnMsg, OK
    else:
        returnMsg = badKeyRequest(False, "Key does not exist", "Error in DELETE")
        return returnMsg, NOT_FOUND

# replica endpoint for getting key value store
@app.route("/get-store", methods=['GET'])  
def store():
    returnMsg = storeMessage(json.dumps(keys))
    return returnMsg, OK

# replica endpoint for getting vector clock
@app.route("/get-clock", methods=['GET'])  
def clock():
    returnMsg = clockMessage(json.dumps(vectorClock))
    return returnMsg, OK

# replica endpoint for getting viewList
@app.route("/get-view", methods=['GET'])
def replicasSeen():
    returnMsg = viewsMessage(json.dumps(viewList))
    return returnMsg, OK

# helper functions for constructing kv json msgs
def putKeyMessage(msg, metaDataString):
    retmsg = jsonify({
        "message":msg,
        "causal-metadata":metaDataString
    })
    return trimMsg(retmsg)

def badKeyRequest(exists, error, msg):
    errorMsg = ""
    if exists == "":
        errorMsg = jsonify({
            "error":error,
            "message":msg
        })
    else:
        errorMsg = jsonify({
            "doesExist":exists,
            "error":error,
            "message":msg
        })
    return trimMsg(errorMsg)

def existsKeyMessage(metaDataString, msg, value):
    getMsg = ""
    if value != "":
        getMsg = jsonify({
            "message":msg,
            "causal-metadata":metaDataString,
            "value":value
        })
    else:
        getMsg = jsonify({
            "doesExist":False,
            "message":msg
        })
    return trimMsg(getMsg)

def serviceError(error, msg):
    retmsg = jsonify({
        "error":error,
        "message":msg
    })
    return trimMsg(retmsg)

# helper functions for constructing view json msgs
def viewMessage(view):
    retmsg = jsonify({
        "message":"View retrieved successfully",
        "view":view
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

def storeMessage(store):
    retmsg = jsonify({
        "message":"Key-Value store retrieved successfully",
        "store":store
    })
    return trimMsg(retmsg)

def clockMessage(clock):
    retmsg = jsonify({
        "message":"Vector clock retrieved successfully",
        "clock":clock
    })
    return trimMsg(retmsg)

def viewsMessage(views):
    retmsg = jsonify({
        "message":"All views retrieved successfully",
        "views":views
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

# method for broadcasting delete for VIEW operations
def broadcastDelete(requestPath, data):
    for addressee in replicasAlive:
        # if the addressee is the current replica or
        # if its down, we goto the next replica
        if addressee == address:
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

# method for broadcasting put for VIEW operations
def broadcastPut(requestPath, data, retrieveStore):
    global keys
    # loop through all addresses seen
    # to check if previously removed addresses have resurrected
    copy = viewList.copy()
    for addressee in copy:
        response = None
        if addressee == address:
            continue
        url = constructURL(addressee, requestPath)
        headers = {"content-type": "application/json"}
        try:
            response = requests.put(url, data=json.dumps(data), headers=headers, timeout=TIME_OUT)
        except:
            app.logger.info(f"Broadcast from {address} => {addressee} failed!")
            pass
        
        if not keys and retrieveStore:
            app.logger.info(f"Attempting to populate Key-Value Store using address: {addressee}")
            newKeys =  getStore(addressee)
            newClock = getClock(addressee)
            if newKeys:
                keys.update(newKeys)

            if newClock:
                vectorClock.update(newClock)

def broadcastPutKey(requestPath, metaDataString, value):
    for addressee in replicasAlive:
        response = None
        if addressee == address:
            continue
        url = constructURL(addressee, requestPath)
        headers = {"content-type": "application/json"}
        try:
            response = requests.put(url, data=json.dumps({"value": value, "causal-metadata": metaDataString}), headers=headers, timeout=0.00001)
        except:
            app.logger.info(f"Broadcast key from {address} => {addressee} failed!")
            pass

def broadcastDeleteKey(requestPath, metaDataString):
    for addressee in replicasAlive:
        response = None
        if addressee == address:
            continue
        url = constructURL(addressee, requestPath)
        headers = {"content-type": "application/json"}
        try:
            response = requests.delete(url, data=json.dumps({"causal-metadata": metaDataString}), headers=headers, timeout=0.00001)
        except:
            app.logger.info(f"Broadcast delete key from {address} => {addressee} failed!")
            pass

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

# sends get request to specified addressee
# for their copy of the key value store
# merging store
def getStore(addressee):
    url = constructURL(addressee, "/get-store")
    headers = {"content-type": "application/json"}
    newKeys = {}
    response = None
    try:
        response = requests.get(url, headers=headers, timeout=TIME_OUT)
    except:
        pass
    if response:
        newKeys = json.loads(json.loads(json.dumps(response.json()))['store'])
        app.logger.info(f"Store accessed at URL: {url}")
    else:
        app.logger.info(f"Store request denied at URL: {url}")
    return newKeys

def getClock(addressee):
    # global vectorClock
    url = constructURL(addressee, "/get-clock")
    headers = {"content-type": "application/json"}
    clock = {}
    response = None
    try:
        response = requests.get(url, headers=headers, timeout=TIME_OUT)
    except:
        pass

    if response:
        clock = json.loads(json.dumps(response.json()))['clock']
        clock = clock.replace("'", "\"")
        clock = json.loads(clock)
        clock = {k: {int(innerKey):v for innerKey, v in clock[k].items()} for k,v in clock.items() }
        app.logger.info(f"Clock accessed at URL: {url}")
    else:
        app.logger.info(f"Clock request denied at URL: {url}")
    return clock
    
    
def compareClocks(clock1, clock2):
    # vector clock comparison
    # 1. for all indexes VC(A) <= VC(B)
    # 2. and VC(A) != VC(B)
    condition1 = list(map(lambda socket: max(clock1.get(socket)) <= max(clock2.get(socket)), clock1))
    condition2 = list(map(lambda socket: max(clock1.get(socket)) != max(clock2.get(socket)), clock1))
    condition3 = list(map(lambda socket: max(clock1.get(socket)) >= max(clock2.get(socket)), clock1))
    concurrent = False

    c1 = all(bool == True for bool in condition1)
    c2 = any(bool == True for bool in condition2)
    c3 = all(bool == True for bool in condition3)

    if c1 and c2:
        return clock2, concurrent
    elif c2 and c3:
        return clock1, concurrent
    elif c2 and not c1 and not c3:
        concurrent = True
        return {}, concurrent

    return {}, False

# def mergeReplicas(newStore, receivedVectorClock):
#     for socket in receivedVectorClock:
#         if receivedVectorClock.get(socket) > vec

# NOTE: need to change all broadcasts to threaded

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
        address = os.environ['SOCKET_ADDRESS']
        view = os.environ['VIEW']
    except:
        pass

    # creating the viewList for later manipulation
    replicasAlive = parseList(view)
    for replica in replicasAlive:
        viewList[replica] = "alive"
        vectorClock[replica] = {0:{}}

    # add command for docker to run the custom server
    manager.add_command('run', CustomServer(host='0.0.0.0', port=8085))
    manager.run()