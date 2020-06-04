from flask import Flask, request, jsonify, redirect
from flask_script import Manager, Server
import os
import sys
import copy
import json
import time
import requests
from random import shuffle, choice
from uhashring import HashRing
import hashlib
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
hashRing = None
sharedResponse = None

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
def shareView(retrieveShard):
    requestPath = "/key-value-store-view"
    data = {"socket-address":socket_address}
    broadcastPut(requestPath, data, retrieveShard)

class CustomServer(Server):
    def __call__(self, app, *args, **kwargs):
        app.logger.info("Instance started!")
        sys.stdout.flush()
        shareView(False)
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
                updateShards = False
                # print("THREAD CHECK:", vectorClock)

                # if the only replica alive is the
                # current replica, we reset the
                if len(replicasAlive) == 1:
                    updateShards = True

                shareView(updateShards)

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
                                # remove the replica from the list
                                replicasAlive.remove(addressee)
                                # remove the replica from the shard
                                if addressee in routing:
                                    shardGroups[routing[addressee]].remove(addressee)
                            except:
                                pass
                            viewList[addressee] = None
                        broadcastDelete(requestPath, data) 
            consoleMessages(SLEEP_TIME)

    thread = threading.Thread(target=check)
    thread.start()

# method for broadcasting put for VIEW operations
def broadcastPut(requestPath, data, retrieveShard):
    global shardGroups
    global routing
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

        if not shardGroups or retrieveShard:
            app.logger.info(f"Attempting to populate shard information using address: {addressee}")
            shardInformation, routingInformation = getShardInformation(addressee)
            if shardInformation:
                shardGroups.update(shardInformation)

            if routingInformation:
                routing.update(routingInformation)


def getShardInformation(addressee):
    url = constructURL(addressee, "/get-shard-information")
    headers = {"content-type": "application/json"}
    shardInformation = {}
    routingInformation = {}
    response = None
    try:
        response = requests.get(url, headers=headers, timeout=TIME_OUT)
    except:
        pass
    if response:
        shardInformation = json.loads(response.json().get('shardInfo'))
        routingInformation = json.loads(response.json().get('routingInfo'))
        shardInformation = {int(k):v for k,v in shardInformation.items()}
        app.logger.info(f"Shard info accessed at URL: {url}")
    else:
        app.logger.info(f"Shard info request denied at URL: {url}")
    return shardInformation, routingInformation

# method for broadcasting delete for VIEW operations
def broadcastDelete(requestPath, data):
    for addressee in replicasAlive:
        # if the addressee is the current replica or
        # if its down, we goto the next replica
        if addressee == socket_address:
            continue
        url = constructURL(addressee, requestPath)
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

def broadcastAddNode(requestPath, data):
    for addressee in replicasAlive:
        response = None
        if addressee == socket_address:
            continue
        url = constructURL(addressee, requestPath)
        headers = {"content-type": "application/json"}
        try:
            response = requests.put(url, data=json.dumps(data), headers=headers, timeout=TIME_OUT)
        except:
            app.logger.info(f"Broadcast put node from {socket_address} => {addressee} failed!")
            pass

# sends get request to specified addressee
# for their copy of the key value store
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

def broadcastPutKey(requestPath, metaDataString, value, shardID):

    shardGroup = shardGroups.get(shardID)

    for addressee in shardGroup:
        response = None
        if addressee == socket_address:
            continue
        url = constructURL(addressee, requestPath)
        headers = {"content-type": "application/json"}
        try:
            response = requests.put(url, data=json.dumps({"value": value, "causal-metadata": metaDataString}), headers=headers, timeout=5)
        except:
            app.logger.info(f"Broadcast PUT key from {socket_address} => {addressee} failed!")
            pass

def broadcastDeleteKey(requestPath, metaDataString, shardID):

    shardGroup = shardGroups.get(shardID)

    for addressee in shardGroup:
        response = None
        if addressee == socket_address:
            continue
        url = constructURL(addressee, requestPath)
        headers = {"content-type": "application/json"}
        try:
            response = requests.delete(url, data=json.dumps({"causal-metadata": metaDataString}), headers=headers, timeout=5)
        except:
            app.logger.info(f"Broadcast delete key from {socket_address} => {addressee} failed!")
            pass

def broadcastReshard(requestPath, data):
    for addressee in replicasAlive:
        if addressee == socket_address:
            continue
        url = constructURL(addressee, requestPath)
        headers = {"content-type": "application/json"}
        try:
            response = requests.put(url, data=json.dumps(data), headers=headers, timeout=5)
        except:
            app.logger.info(f"Broadcast reshard from {socket_address} => {addressee} failed!")
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

# endpoint for requests used to check dead replicas
@app.route("/broadcast-get", methods=['GET'])
def getCheck():
    returnMsg = jsonify({
        "message":"Replica Alive!"
    })
    return trimMsg(returnMsg), OK

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
        shardGroups[routing.get(addressToDelete)].remove(addressToDelete)
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
        if addressToPut in routing:
            shardGroups[routing[addressToPut]].append(addressToPut)
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
    # print("GORUP", shardGroup)
    sys.stdout.flush()
    returnMsg = shardGroupMessage(json.dumps(shardGroup))
    return returnMsg, OK

@app.route("/key-value-store-shard/shard-id-key-count/<id>", methods=['GET'])
def getShardKeyCount(id):
    shardGroup = shardGroups.get(int(id))
    numKeys = None

    print("Group!!!!!!!!!!!!!!!!!!",shardGroup)
    sys.stdout.flush()

    # if the current socket address is part of the shard,
    # we simply get the number of items in the key dict
    if socket_address in shardGroup:
        if keys.items():
            numKeys = len([x for x in keys.values() if x])
        else:
            numKeys = 0
    # else, we have to request it from one of the replicas in the shard
    # lets just use the first one in the group
    else:
        for addressee in shardGroup:
            if not numKeys:
                url = constructURL(addressee, "/get-key-count")
                headers = {"content-type": "application/json"}
                response = None
                try:
                    response = requests.get(url, headers, timeout=TIME_OUT)
                except:
                    pass

                #needs to be tested
                if response:
                    numKeys = int(response.json().get('keycount'))
        
    if numKeys == None:
        app.logger.info(f"All replicas of shard {id} are down!")
        return "", NOT_FOUND

    returnMsg = shardKeyCountMessage(numKeys)
    return returnMsg, OK

@app.route("/get-key-count", methods=['GET'])
def getKeyCount():
    global keys
    keyCount = len([x for x in keys.values() if x])
    returnMsg = keyCountMessage(str(keyCount))
    return returnMsg, OK

@app.route("/key-value-store-shard/add-member/<id>", methods=['PUT'])
def addNode(id):
    data = request.get_json()
    addressToPut = data['socket-address']
    id = int(id)
    if id in shardGroups:
        shardGroups.get(id).append(addressToPut)
        routing[addressToPut] = id

        # get the shard of the current replica
        shardGroup = shardGroups.get(routing.get(socket_address))
        inGroup = addressToPut in shardGroup

        # we only add nodes to the VC that are:
        # not in the vector clock but belong in the same shard
        if addressToPut not in vectorClock and inGroup:
            vectorClock[addressToPut] = {0:{}}

        requestPath = "/key-value-store-shard/add-member-broadcast/" + str(id)
        with dataLock:
            threading.Thread(target=broadcastAddNode, args=(requestPath, data,)).start()

    return "", OK

@app.route("/key-value-store-shard/add-member-broadcast/<id>", methods=['PUT'])
def addNodeBroadcast(id):
    global keys
    global vectorClock
    global hashRing
    id = int(id)
    data = request.get_json()
    addressToPut = data['socket-address']
    
    # get the shard group of the id to add to
    shardGroup = shardGroups.get(id)

    # add the address to the shard
    shardGroup.append(addressToPut)
    routing[addressToPut] = id

    # we also want to add it to the vector clock of the shard
    # we only add nodes to the VC that are:
    # not in the vector clock but belong in the same shard

    # get the shard of the current replica
    group = shardGroups.get(routing.get(socket_address))
    inGroup = addressToPut in group

    if addressToPut not in vectorClock and inGroup:
        vectorClock[addressToPut] = {0:{}}
    
    # if the current replica is adding itself to a shard,
    # we also get the kvs of the shard to complete the addition
    # of the node to the shard
    if addressToPut == socket_address:
        hashRing = HashRing(nodes=list(shardGroups.keys()))

        for addressee in shardGroup:

            # since we added the new node, we don't ask itself
            if addressee == socket_address:
                continue

            if not keys:
                app.logger.info(f"Attempting to populate Key-Value Store using address: {addressee}")
                newKeys = getStore(addressee)
                newClock = getClock(addressee)
                if newKeys:
                    keys.update(newKeys)
                if newClock:
                    vectorClock.update(newClock)

    return "", OK

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

@app.route("/shard-error")
def shardError():
    returnMsg = shardErrorMessage()
    return returnMsg, BAD_REQUEST

@app.route("/get-shard-information")
def shardInfoEndpoint():
    returnMsg = shardInfoMessage(json.dumps(shardGroups), json.dumps(routing))
    return returnMsg, OK

# QUESTION: SHOULD GET RETURN ITS OWN VECTOR CLOCK OR THE VC FROM THE REPLICA THAT IT FORWARDED THE REQUEST TO?
@app.route("/key-value-store/<key>", methods=['GET'])
def getKey(key):

    # initial value to None, since we 
    # don't know if the key actually exists
    # in the kvs
    value = None
    retrieved = False
    doesExist = True
    msg = ""
    error = ""

    # get the shard id of the key
    shardID = hashRing.get_node(key)

    # if key belongs to the shard of the current replica
    if routing.get(socket_address) == shardID:
        # we get the key from the store
        if key in keys and keys[key] != None:
            value = keys[key]

    # else, we ask it from the proper shard
    else:
        # by getting the keys shard and looping through the addresses
        shardGroup = shardGroups.get(shardID)
        shuffle(shardGroup)
        for addressee in shardGroup:
            if not retrieved:
                url = constructURL(addressee, request.path)
                headers = {"content-type": "application/json"}
                response = None
                try:
                    response = requests.get(url, headers, timeout=10)
                    retrieved = True
                except:
                    app.logger.info(f"Forward GET request from {socket_address} => {addressee} failed!")
                    pass

                # return the forwarded response back to the client
                if retrieved:
                    metaDataString = response.json().get('causal-metadata')
                    value = response.json().get('value')
                    doesExist = response.json().get('doesExist')
                    error = response.json().get('error')
                    msg = response.json().get('message')
            else:
                break
        
        if not retrieved:
            return "Cannot Forward GET!!", NOT_FOUND
        else:
            rc = response.status_code
            if rc == OK:
                returnMsg = existsKeyMessage(metaDataString, msg, value)
                return returnMsg, rc
            else:
                returnMsg = badKeyRequest(doesExist, error, msg)
                return returnMsg, rc
                

    if value != None:
        returnMsg = existsKeyMessage(json.dumps(vectorClock), "Retrieved successfully", value)
        return returnMsg, OK
    else:
        returnMsg = badKeyRequest(False, "Key does not exist", "Error in GET")
        return returnMsg, NOT_FOUND

@app.route("/key-value-store/<key>", methods=['DELETE'])
def deleteKey(key):
    global vectorClock
    data = request.get_json()

    # get data
    metaDataString = data['causal-metadata']
    msg = None
    id = None
    doesExist = True
    retrieved = False
    error = ""

    # get the shard id of the key
    shardID = hashRing.get_node(key)

    # if the current node does not belong to the shard, we forward the request
    if routing.get(socket_address) != shardID:
        shardGroup = shardGroups.get(shardID)
        response = None
        shuffle(shardGroup)
        for addressee in shardGroup:
            if not retrieved:
                url = constructURL(addressee, request.path)
                headers = {"content-type": "application/json"}
                try:
                    response = requests.delete(url, data=json.dumps({"causal-metadata": metaDataString}), headers=headers, timeout=5)
                    retrieved = True
                except:
                    app.logger.info(f"Forward DELETE request from {socket_address} => {addressee} failed!")
                    pass
            
                if retrieved:
                    msg = response.json().get('message')
                    id = response.json().get('shard-id')
                    metaDataString = response.json().get('causal-metadata')
                    error = response.json().get('error')
                    doesExist = response.json().get('doesExist')
            else:
                break

        if not retrieved:
            return "Cannot Forward!!", NOT_FOUND
        else:
            rc = response.status_code
            if rc == OK:
                return putKeyMessage(msg, metaDataString, id), rc
            else:
                return badKeyRequest(doesExist, error, msg), rc
    # else if the node does belong to the shard, we start the operation
    else:
        if metaDataString == "":
            app.logger.info("------FIRST DELETE------")
        else:
            metaDataString = metaDataString.replace("'", "\"")
            receivedVectorClock = json.loads(metaDataString)
            receivedVectorClock = {k: {int(innerKey):v for innerKey, v in receivedVectorClock[k].items()} for k,v in receivedVectorClock.items() }

            biggerClock, concurrent = compareClocks(vectorClock, receivedVectorClock, shardID)

            if vectorClock == biggerClock:
                pass

            elif receivedVectorClock == biggerClock:
                for socket in receivedVectorClock.keys():
                    if routing.get(socket) != shardID:
                        continue
                    upToDate = max(receivedVectorClock[socket])
                    kvPair = receivedVectorClock[socket][upToDate]
                    keys.update(kvPair)

                vectorClock.update(receivedVectorClock)

            elif concurrent:
                for socket in receivedVectorClock.keys():
                    if routing.get(socket) != shardID:
                        continue
                    upToDate = max(receivedVectorClock[socket])
                    localHigh = max(vectorClock[socket])
                    if upToDate > localHigh:
                        kvPair = receivedVectorClock[socket][upToDate]
                        keys.update(kvPair)
                        vectorClock[socket] = receivedVectorClock.get(socket)
        
        # now that causal consistency has been established
        # we start the actual operation

        # if the key exists in the shard and it hasn't previously 
        # been deleted
        if key in keys and keys[key] != None:
            keys[key] = None

            versions = vectorClock.get(socket_address)
            merged = {**vectorClock[socket_address][max(versions)], **{key:None}}
            vectorClock[socket_address].update({(max(versions) + 1): merged })

            metaDataString = json.dumps(vectorClock)

            requestPath = "/delete-key-broadcast/" + key

            with dataLock:
                threading.Thread(target = broadcastDeleteKey, args=(requestPath, metaDataString, shardID,)).start()
            
            returnMsg = putKeyMessage("Deleted successfully", metaDataString, shardID)
            return returnMsg, OK
        else:
            returnMsg = badKeyRequest(False, "Key does not exist", "Error in DELETE")
            return returnMsg, NOT_FOUND
        

@app.route("/delete-key-broadcast/<key>", methods=['DELETE'])
def deleteKeyBroadcast(key):
    global vectorClock
    data = request.get_json()

    # get data
    metaDataString = data['causal-metadata']

    # verify causal consistency
    if metaDataString != "":
        metaDataString = metaDataString.replace("'", "\"")
        receivedVectorClock = json.loads(metaDataString)
        receivedVectorClock = {k: {int(innerKey):v for innerKey, v in receivedVectorClock[k].items()} for k,v in receivedVectorClock.items() }

        biggerClock, concurrent = compareClocks(vectorClock, receivedVectorClock, routing.get(socket_address))

        if vectorClock == biggerClock:
            pass

        elif receivedVectorClock == biggerClock:
            for socket in receivedVectorClock.keys():
                if routing.get(socket) != routing.get(socket_address):
                    continue
                upToDate = max(receivedVectorClock[socket])
                kvPair = receivedVectorClock[socket][upToDate]
                keys.update(kvPair)

            vectorClock.update(receivedVectorClock)

        elif concurrent:
            for socket in receivedVectorClock.keys():
                if routing.get(socket) != routing.get(socket_address):
                    continue
                upToDate = max(receivedVectorClock[socket])
                localHigh = max(vectorClock[socket])
                if upToDate > localHigh:
                    kvPair = receivedVectorClock[socket][upToDate]
                    keys.update(kvPair)
                    vectorClock[socket] = receivedVectorClock.get(socket)
    
    metaDataString = json.dumps(vectorClock)

    if key in keys or keys[key] != None:
        keys[key] = None
        returnMsg = putKeyMessage("Deleted successfully", metaDataString, routing.get(socket_address))
        return returnMsg, OK
    else:
        returnMsg = badKeyRequest(False, "Key does not exist", "Error in DELETE")
        return returnMsg, NOT_FOUND

@app.route("/key-value-store/<key>", methods=['PUT'])
def putKey(key):
    global vectorClock
    newKey = False
    data = request.get_json()

    # get data
    value = data['value']
    metaDataString = data['causal-metadata']
    msg = None
    id = None
    retrieved = False

    # check invalid request
    if not value:
        returnMsg = badKeyRequest("", "Value is missing", "Error in PUT")
        return returnMsg, BAD_REQUEST

    # get the shard id of the key
    shardID = hashRing.get_node(key)

    # if the node does not belong to the shard, we forward the request
    if routing.get(socket_address) != shardID:
        # by getting the proper shard and looping through the addresses
        shardGroup = shardGroups.get(shardID)
        response = None
        shuffle(shardGroup)
        for addressee in shardGroup:
            if not retrieved:
                url = constructURL(addressee, request.path)
                headers = {"content-type": "application/json"}
                try:
                    response = requests.put(url, data=json.dumps({"value": value, "causal-metadata": metaDataString}), headers=headers, timeout=5)
                    retrieved = True
                except:
                    app.logger.info(f"Forward PUT request from {socket_address} => {addressee} failed!")
                    pass
                
                # return the forwarded messages response to the client
                if retrieved:
                    msg = response.json().get('message')
                    metaDataString = response.json().get('causal-metadata')
                    id = response.json().get('shard-id')
            else:
                break

        # messages that are failed to be forwarded are simply rejected
        if not retrieved:
            return "Cannot Forward!!", NOT_FOUND
        else:
            returnMsg = putKeyMessage(msg, metaDataString, id)
            return returnMsg, response.status_code
    # else if the node does belong to the shard, we start the operation
    else:  

        # by verifying causal consistency
        if metaDataString == "":
            app.logger.info("------FIRST PUT------")
        else:
            metaDataString = metaDataString.replace("'", "\"")
            receivedVectorClock = json.loads(metaDataString)
            receivedVectorClock = {k: {int(innerKey):v for innerKey, v in receivedVectorClock[k].items()} for k,v in receivedVectorClock.items() }
            biggerClock, concurrent = compareClocks(vectorClock, receivedVectorClock, shardID)

            # if the local clock is strictly bigger
            if vectorClock == biggerClock:
                pass
            # if the recieved clock is stricly bigger
            elif receivedVectorClock == biggerClock:
                # merge clocks
                for socket in receivedVectorClock.keys():
                    if routing.get(socket) != shardID:
                        continue
                    upToDate = max(receivedVectorClock[socket])
                    kvPair = receivedVectorClock[socket][upToDate]
                    # print(kvPair)
                    sys.stdout.flush()
                    keys.update(kvPair)

                # merge vector clocks
                vectorClock.update(receivedVectorClock)

            elif concurrent:
                # merge clocks and store based on highest value
                for socket in receivedVectorClock.keys():
                    if routing.get(socket) != shardID:
                        continue
                    upToDate = max(receivedVectorClock[socket])
                    localHigh = max(vectorClock[socket])
                    if upToDate > localHigh:
                        kvPair = receivedVectorClock[socket][upToDate]
                        keys.update(kvPair)
                        vectorClock[socket] = receivedVectorClock.get(socket)
    
        # now that causal consistency has been established
        # we start the actual operation

        # its a new key if it doesn't exist in the store
        # None means that it was deleted previously, so we check that too
        if key not in keys or keys[key] == None:
            newKey = True
        
        # add the key
        keys[key] = value

        # increment the vector clock and update causal-metadata
        versions = vectorClock.get(socket_address)
        merged = {**vectorClock[socket_address][max(versions)], **{key:value}}
        vectorClock[socket_address].update({(max(versions) + 1): merged })
        metaDataString = json.dumps(vectorClock)

        # create a thread to broadcast the PUT operation to the shard
        requestPath = "/key-broadcast/" + key
        with dataLock:
            threading.Thread(target = broadcastPutKey, args=(requestPath, metaDataString, value, shardID)).start()
        
        # and output the appropriate return message
        if newKey:
            returnMsg = putKeyMessage("Added successfully", metaDataString, shardID)
            return returnMsg, CREATED
        else:
            returnMsg = putKeyMessage("Updated successfully", metaDataString, shardID)
            return returnMsg, OK

@app.route("/key-broadcast/<key>", methods=['PUT'])
def putKeyBroadcast(key):
    global vectorClock
    data = request.get_json()

    # get data
    value = data['value']
    metaDataString = data['causal-metadata']

    # check invalid request
    if not value:
        returnMsg = badKeyRequest("", "Value is missing", "Error in PUT")
        return returnMsg, BAD_REQUEST

    # broadcasts are guaranteed to be within a shard,
    # so we don't need to check

    # once again, we need to check for causal violations
    # before applying the operation
    if metaDataString != "":
        metaDataString = metaDataString.replace("'", "\"")
        receivedVectorClock = json.loads(metaDataString)
        receivedVectorClock = {k: {int(innerKey):v for innerKey, v in receivedVectorClock[k].items()} for k,v in receivedVectorClock.items() }

        biggerClock, concurrent = compareClocks(vectorClock, receivedVectorClock, routing.get(socket_address))

        # if the local clock is strictly bigger which should NEVER happen since its a broadcast
        if vectorClock == biggerClock:
            pass
        # if the recieved clock is stricly bigger
        elif receivedVectorClock == biggerClock:
            for socket in receivedVectorClock.keys():
                if routing.get(socket) != routing.get(socket_address):
                    continue
                upToDate = max(receivedVectorClock[socket])
                kvPair = receivedVectorClock[socket][upToDate]
                keys.update(kvPair)

            vectorClock.update(receivedVectorClock)
            
        elif concurrent:
            for socket in receivedVectorClock.keys():
                if routing.get(socket) != routing.get(socket_address):
                    continue
                upToDate = max(receivedVectorClock[socket])
                localHigh = max(vectorClock[socket])
                if upToDate > localHigh:
                    kvPair = receivedVectorClock[socket][upToDate]
                    keys.update(kvPair)
                    vectorClock[socket] = receivedVectorClock.get(socket)

    metaDataString = json.dumps(vectorClock)

    # check if its a new key or as been previously deleted
    if key not in keys or keys[key] == None:
        # perform operation
        keys[key] = value
        returnMsg = putKeyMessage("Added successfully", metaDataString, routing.get(socket_address))
        return returnMsg, CREATED
    else:
        # else update the existing key's value
        keys[key] = value
        returnMsg = putKeyMessage("Updated successfully", metaDataString, routing.get(socket_address))
        return returnMsg, OK

@app.route("/key-value-store-shard/reshard", methods=['PUT'])
def reshardKVS():
    global shard_count
    global shardGroups
    global vectorClock

    valid = False
    fromClient = False

    data = request.get_json()

    source = request.remote_addr + ":8085"
    if source not in viewList:
        fromClient = True

    # shard redistribution
    resharded =  {}
    extraNodes = []

    for shardID in shardGroups:
        shardGroups.get(shardID).sort()

    # get the shards
    items = shardGroups.items()

    # reset vector clocks
    vectorClock = {}

    # get data
    shard_count = data['shard-count']
    currentShardCount = len(list(shardGroups.keys()))

    nodesPerShard = len(replicasAlive) // shard_count

    if nodesPerShard < 2:
        returnMsg = shardErrorMessage()
        return returnMsg, BAD_REQUEST 

    original = copy.deepcopy(shardGroups)

    # if the shard count is reduced
    if shard_count < currentShardCount:

        print("CURRENT DISTRIBUTION", shardGroups)
        sys.stdout.flush()

        # delete the smallest shard and move its addresses
        # to the rest
        while(currentShardCount - shard_count != 0):
            smallestShard = getSmallestShard(shardGroups.items())

            for node in shardGroups[smallestShard]:
                routing[node] = None

            extraNodes += shardGroups[smallestShard]
            del shardGroups[smallestShard]
            hashRing.remove_node(smallestShard)
            currentShardCount-=1

        print("REDUCED SHARDS", shardGroups)
        print("EXTRA NODES", extraNodes)
        sys.stdout.flush()

        extraNodes.sort()

        print("SORTING EXTRA NODES", extraNodes)
        sys.stdout.flush()

        for shard in shardGroups.items():
            if len(shard[1]) < 2:
                while len(shard[1]) < 2:
                    if extraNodes:
                        node = extraNodes.pop()
                        shardGroups[shard[0]].append(extraNodes.pop())
                        routing[node] = shard[0]
                    if not extraNodes:
                        break


        print("FULFILLING FAULT TOLERANT", resharded, "EXTRA NODES", extraNodes)
        sys.stdout.flush()

        while extraNodes:
            node = extraNodes.pop()
            if routing[node]:
                shardGroups[routing[node]].append(node)
            else:
                hashShard = hashRing.get_node(node)
                shardGroups[hashShard].append(node)
                routing[node] = hashShard

        print("USING EXTRA NODES", resharded)
        sys.stdout.flush()

        for shardID in shardGroups.keys():
            shardGroups[shardID].sort()
        
        print("SORTING RESHARDED", resharded)
        sys.stdout.flush()

        valid = verifyShardLength(shardGroups.values())

        if not valid:
            shardGroups = original



    # if the shard count has increased
    elif shard_count > currentShardCount:
        # redistribute addresses, while maintaining
        # 2 minimum nodes in each shard

        print("CURRENT DISTRIBUTION", shardGroups)


        # transfer shardID's to new distribution
        for shardID in shardGroups:
            resharded[shardID] = []
            print("EXISTING ID", resharded)
            sys.stdout.flush()

        # add the new shard ID's that are required
        if shard_count > len(shardGroups.items()):
            # required shard_count - previous shard_count
            numAddIDs = shard_count - len(shardGroups.items())
            for _ in range(numAddIDs):
                # add it
                newID = max(resharded.keys()) + 1
                resharded[newID] = []
                hashRing.add_node(newID)
            
            print("NEW IDs", resharded)
            sys.stdout.flush()

        # transfer 2 nodes of each shard from the previous
        # shard distribution to reduce key transfers
        for shard in shardGroups.keys():
            shardGroup = shardGroups.get(shard)
            shardGroup.sort()
            for i in range(len(shardGroup)):
                # if the length of the shard has already satisfied'
                # the 2 node requirement...
                if len(resharded[shard]) == 2:
                    # append it to a list of extra nodes
                    extraNodes.append(shardGroup[i])
                else:
                    # else, try to reach the requirement
                    resharded[shard].append(shardGroup[i])

        print("TAKE BASE 2", resharded)
        sys.stdout.flush()

        print("EXTRA NODES", extraNodes)
        sys.stdout.flush()

        extraNodes.sort()

        print("SORTING EXTRA NODES", extraNodes)
        sys.stdout.flush()

        # fulfill any shards that are not fault tolerant
        # using the extra nodes
        for shard in resharded.items():
            if len(shard[1]) < 2:
                while len(shard[1]) < 2:
                    if extraNodes:
                        exnode = extraNodes.pop()
                        resharded[shard[0]].append(exnode)
                        routing[exnode] = shard[0]
                    if not extraNodes:
                        break

        print("FULFILLING FAULT TOLERANT", resharded, "EXTRA NODES", extraNodes)
        sys.stdout.flush()

        # assign any extraneous nodes
        # prioritized by their original shard ID/group
        # else, assign at random
        while extraNodes:
            node = extraNodes.pop()
            if node in routing:
                resharded[routing[node]].append(node)
            else:
                hashShard = hashRing.get_node(node)
                shardGroups[hashShard].append(node)
                routing[node] = hashShard

        print("USING EXTRA NODES", resharded)
        sys.stdout.flush()

        for shardID in resharded.keys():
            resharded[shardID].sort()

        print("SORTING RESHARDED", resharded)
        sys.stdout.flush()

        # validate fault tolerancy
        valid = verifyShardLength(resharded.values())

        if valid:
            # reassign the shards to the new distribution
            shardGroups = resharded
    else:
        valid = verifyShardLength(shardGroups.values())
        if not valid:

            print("CURRENT DISTRIBUTION", shardGroups)
            sys.stdout.flush()

            for shardID in shardGroups:
                resharded[shardID] = []
                print("EXISTING ID", resharded)
                sys.stdout.flush()

            for shard in shardGroups.keys():
                shardGroup = shardGroups.get(shard)
                for i in range(len(shardGroup)):
                    # if the length of the shard has already satisfied'
                    # the 2 node requirement...
                    if len(resharded[shard]) == 2:
                        # append it to a list of extra nodes
                        extraNodes.append(shardGroup[i])
                    else:
                        # else, try to reach the requirement
                        resharded[shard].append(shardGroup[i])

            print("TAKE BASE 2", resharded)
            sys.stdout.flush()

            print("EXTRA NODES", extraNodes)
            sys.stdout.flush()

            extraNodes.sort()

            print("SORTING EXTRA NODES", extraNodes)
            sys.stdout.flush()

            for shard in resharded.items():
                if len(shard[1]) < 2:
                    while len(shard[1]) < 2:
                        if extraNodes:
                            exnode = extraNodes.pop()
                            resharded[shard[0]].append(exnode)
                            routing[exnode] = shard[0]
                        if not extraNodes:
                            break

            print("FULFILLING FAULT TOLERANT", resharded, "EXTRA NODES", extraNodes)
            sys.stdout.flush()

            while extraNodes:
                node = extraNodes.pop()
                if node in routing:
                    resharded[routing[node]].append(node)
                else:
                    hashShard = hashRing.get_node(node)
                    shardGroups[hashShard].append(node)
                    routing[node] = hashShard

            print("USING EXTRA NODES", resharded)
            sys.stdout.flush()

            for shardID in resharded.keys():
                resharded[shardID].sort()

            print("SORTING RESHARDED", resharded)
            sys.stdout.flush()

            # validate fault tolerancy
            valid = verifyShardLength(resharded.values())

            if valid:
                # reassign the shards to the new distribution
                shardGroups = resharded

    # if the redistribution is successfull
    if valid:
        if fromClient:
            with dataLock:
                threading.Thread(target=broadcastReshard, args=(request.path, data,)).start()

        if shardGroups != original:
            print("REDISTRIBUTING KEYS")
            sys.stdout.flush()
            copyKeys = copy.deepcopy(keys)

            if "10.10.0.3:8085" == socket_address:
                print("TESTING, 1, ,2 ,3", shardGroups.get(routing.get(socket_address)))

            for key in copyKeys:
                value = keys[key]
                shardID = hashRing.get_node(key)
                if socket_address not in shardGroups.get(shardID):
                    del keys[key]

                requestPath = "/key-broadcast/" + key
                metaDataString = ""
                with dataLock:
                    threading.Thread(target = broadcastPutKey, args=(requestPath, metaDataString, value, shardID)).start()

        returnMsg = reshardSuccessfull()
        return returnMsg, OK
    else:
        if fromClient:
            print("FROM CLIENT")
        else:
            print("FROM ANOTHER REPLICA")

        returnMsg = shardErrorMessage()
        return returnMsg, BAD_REQUEST 

def getSmallestShard(items):
    targetShard = None
    minShard = float('inf')
    for shard in items:
        shardKey = shard[0]
        shardNodes = shard[1]
        if len(shardNodes) < minShard:
            targetShard = shardKey
            minShard = len(shardNodes)

    return targetShard

def verifyShardLength(values):
    return all(b == True for b in list(map(lambda x: len(x) >= shard_count, values)))

# helper functions for constructing kv json msgs
def putKeyMessage(msg, metaDataString, id):
    retmsg = jsonify({
        "message":msg,
        "causal-metadata":metaDataString,
        "shard-id":id
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

def shardErrorMessage():
    retmsg = jsonify({
        "message":"Not enough nodes to provide fault-tolerance with the given shard count!"
    })
    return trimMsg(retmsg)

def reshardSuccessfull():
    retmsg = jsonify({
        "message":"Resharding done successfully"
    })
    return trimMsg(retmsg)

def keyCountMessage(numKeys):
    retmsg = jsonify({
        "message":"Key Count retrieved successfully",
        "keycount":numKeys
    })
    return trimMsg(retmsg)

def shardInfoMessage(groups, routings):
    retmsg = jsonify({
        "message":"Shard information successfully retrieved",
        "shardInfo":groups,
        "routingInfo":routings
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

def compareClocks(clock1, clock2, shardID):
    # vector clock comparison
    # 1. for all indexes VC(A) <= VC(B)
    # 2. and VC(A) != VC(B)

    shardGroup = shardGroups.get(shardID)

    copyClock1 = copy.deepcopy(clock1)
    copyClock2 = copy.deepcopy(clock2)

    for socket in clock1:
        if socket not in shardGroup:
            del copyClock1[socket]
            del copyClock2[socket]

    condition1 = list(map(lambda socket: max(copyClock1.get(socket)) <= max(copyClock2.get(socket)), copyClock1))
    condition2 = list(map(lambda socket: max(copyClock1.get(socket)) != max(copyClock2.get(socket)), copyClock1))
    condition3 = list(map(lambda socket: max(copyClock1.get(socket)) >= max(copyClock2.get(socket)), copyClock1))
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

def verifyShardLength(values):
    return all(b == True for b in list(map(lambda x: len(x) >= 2, values)))

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

    # populate vector clocks and view history
    for replica in replicasAlive:
        viewList[replica] = "alive"

    # we need to check if the shard env variable is provided first
    # if it is, that means that the replica is not a instance
    # that was added later, so we create the shard groups
    if shard_count != 0:

        nodesPerShard = len(replicasAlive) // shard_count

        # if the SHARD_COUNT variable is too large, we exit
        if nodesPerShard < 2:
            url = constructURL(socket_address, "/shard-error")
            redirect(url, code=302)
            sys.exit("Invalid Docker Command: SHARD_COUNT")

        for i, replica in enumerate(replicasAlive):
            shardID = i%shard_count
            if shardID in shardGroups:
                shardGroups[shardID].append(replica)
            else:
                shardGroups[shardID] = [replica] 
            routing[replica] = shardID

        # only populate vector clocks of the same shard 
        shardGroup = shardGroups.get(routing.get(socket_address))
        hashRing = HashRing(nodes=list(shardGroups.keys()))
        for replica in replicasAlive:
            vectorClock[replica] = {0:{}}

        print("DISTRIBUTION", hashRing.distribution)
        sys.stdout.flush()
        

    # add command for docker to run the custom server
    manager.add_command('run', CustomServer(host='0.0.0.0', port=8085, threaded=True))
    manager.run()
