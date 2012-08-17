# The module that implements the server, the clients, and the communication
# between the server and clients.
#
# The communication part of this code is adapted from the original mincemeat 
# code. For more details, please read the LICENSE file.
#
# author: Yangqing Jia (jiayq84@gmail.com)

# python modules
import argparse
import asynchat
import asyncore
import cPickle as pickle
import gflags
import hashlib
import hmac
import logging
import marshal
import optparse
import os
import random
import socket
import sys
import types

# local modules
import mapreducer

# constant variables
VERSION = "0.0.1"
SEPARATOR = ':'
TERMINATOR = '\n'

class Enum(set):
    def __getattr__(self, name):
        if name in self:
            return name
        raise AttributeError

# we use an enum to define the commands, just in case some typo takes place
# in coding, and shortens the command being send over the network (really
# doesn't matter much in the modern days...)
COMMAND = Enum(['challenge',
                'auth',
                'disconnect',
                'map',
                'reduce',
                'mapdone',
                'reducedone',
               ])

TASK = Enum(['START',
             'MAPPING',
             'REDUCING',
             'FINISHED',
            ])

# commandline flags
FLAGS = gflags.FLAGS
# general flags    
gflags.DEFINE_boolean("server", False,
                      "If --server is specified, run in servermode.")
# flags defined by Protocol
gflags.DEFINE_string("password", "default",
                     "The password for server client authentication")
gflags.DEFINE_integer("port", 11235, 
                      "The port number for the mapreduce task")
# flags defined by Client
gflags.DEFINE_string("mapper", "BasicMapper",
                     "The mapper class for the mapreduce task")
gflags.DEFINE_string("reducer", "BasicReducer",
                     "The reducer class for the mapreduce task")
gflags.DEFINE_string("address", "127.0.0.1",
                     "The address of the server")
# flags defined by Server
gflags.DEFINE_string("reader", "BasicReader",
                     "The reader class for the mapreduce task")


class Protocol(asynchat.async_chat, object):
    """Communication protocol
    
    The Protocol class defines the basic protocol that both the server and
    the client will follow during the mapreduce execution.

    The basic function the protocol implements are:
        * provide parser for  the basic password and port for the connection
        * send command with possible arguments and data
        * deal with incoming data
        * Two-way authentication
    """
    def __init__(self, conn=None):
        if conn:
            asynchat.async_chat.__init__(self, conn)
        else:
            asynchat.async_chat.__init__(self)
        # We use the newline as the terminator
        self.set_terminator(TERMINATOR)
        self.buffer = []
        self.auth = None
        self.mid_command = None

    def collect_incoming_data(self, data):
        """Collect the incoming data and put it under buffer
        """
        self.buffer.append(data)

    def send_command(self, command, data=None, arg=None):
        """Send the command with optional data
        """
        encoded = command + SEPARATOR
        if arg:
            # this command contains some arguments
            encoded += arg
            #logging.debug("<- " + encoded)
            self.push(encoded + TERMINATOR)
        elif data:
            # this command contains pickled data
            pdata = pickle.dumps(data)
            encoded += str(len(pdata))
            #logging.debug("<- " + encoded + " (pickle)")
            self.push(encoded + TERMINATOR + pdata)
        else:
            #logging.debug("<- " + encoded)
            self.push(encoded + TERMINATOR)

    def decode_command(self, message):
        """decode the command to the command and the data
        """
        idx = message.find(SEPARATOR)
        if idx == -1:
            raise ValueError, "Unrecognized command: " + message
        return message[:idx], message[idx+1:]

    def found_terminator(self):
        message = "".join(self.buffer)
        if not self.auth == "Done":
            # before authentication, call process_unauthed_command
            command, data = self.decode_command(message)
            self.process_unauthed_command(command, data)
        elif self.mid_command is None:
            # if we are not in the middle of a command, we read the command
            # and process it, and also check if this command comes with a
            # data string
            command, length = self.decode_command(message)
            if command == COMMAND.challenge:
                # deal with challenge string
                #logging.debug("-> " + message)
                self.process_command(command, length)
            elif length:
                # this command comes with a data string: obtain the data
                #logging.debug("-> " + message + " (pickle)")
                self.set_terminator(int(length))
                self.mid_command = command
            else:
                # otherwise, simply process this command
                self.process_command(command)
        else:
            # Read the data segment from the previous command
            if not self.auth == "Done":
                logging.fatal("Recieved pickled data from unauthed source")
                sys.exit(1)
            data = pickle.loads(message)
            # reset the terminator and mid_command for the next command
            self.set_terminator(TERMINATOR)
            command = self.mid_command
            self.mid_command = None
            self.process_command(command, data)
        # clean the buffer
        self.buffer = []

    def send_challenge(self):
        self.auth = os.urandom(20).encode("hex")
        self.send_command(COMMAND.challenge, arg=self.auth)

    def respond_to_challenge(self, command, data):
        mac = hmac.new(FLAGS.password, data, hashlib.sha1)
        self.send_command(COMMAND.auth, arg=mac.digest().encode("hex"))
        self.post_auth_init()

    def verify_auth(self, command, data):
        mac = hmac.new(FLAGS.password, self.auth, hashlib.sha1)
        if data == mac.digest().encode("hex"):
            self.auth = "Done"
            logging.info("Authenticated the other end")
        else:
            self.handle_close()

    def process_command(self, command, data=None):
        handlers = {
            COMMAND.challenge: self.respond_to_challenge,
            COMMAND.disconnect: lambda x,y: self.handle_close(),
            }
        try:
            handlers[command](command, data)
        except KeyError:
            logging.critical("Unknown command received: " + command) 
            self.handle_close()

    def process_unauthed_command(self, command, data=None):
        handlers = {
            COMMAND.challenge: self.respond_to_challenge,
            COMMAND.auth: self.verify_auth,
            COMMAND.disconnect: lambda x, y: self.handle_close(),
            }
        try:
            handlers[command](command, data)
        except KeyError:
            logging.critical("Unknown command received: " + command) 
            self.handle_close()
        

class Client(Protocol):
    def __init__(self):
        Protocol.__init__(self)
        self.mapper = None
        self.reducer = None

    def run_client(self):
        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.connect((FLAGS.address, FLAGS.port))
        asyncore.loop()

    def handle_connect(self):
        pass

    def handle_close(self):
        self.close()

    def call_map(self, command, data):
        """Calls the map function.
        
        Input:
            command: a dummy variable that equals to COMMAND.map
            data: a tuple containing the (key,value) pair
        """
        logging.info("Mapping %s" % str(data[0]))
        results = {}
        if self.mapper is None:
            # create the mapper instance
            self.mapper = mapreducer.Mapper(FLAGS.mapper)()
        for k, v in self.mapper.Map(data[0], data[1]):
            try:
                results[k].append(v)
            except KeyError:
                results[k] = [v]
        self.send_command(COMMAND.mapdone, (data[0], results))

    def call_reduce(self, command, data):
        """Calls the reduce function.

        See call_map for details
        """
        logging.info("Reducing %s" % str(data[0]))
        if self.reducer is None:
            # create the reducer instance
            self.reducer = mapreducer.Reducer(FLAGS.reducer)()
        results = self.reducer.Reduce(data[0], data[1])
        self.send_command(COMMAND.reducedone, (data[0], results))
        
    def process_command(self, command, data=None):
        handlers = {
            COMMAND.map: self.call_map,
            COMMAND.reduce: self.call_reduce,
            }
        try:
            handlers[command](command, data)
        except KeyError:
            # If key not recognized, fall back to the super class
            Protocol.process_command(self, command, data)

    def post_auth_init(self):
        if not self.auth:
            self.send_challenge()


class Server(asyncore.dispatcher, object):
    def __init__(self):
        asyncore.dispatcher.__init__(self)
        self._datasource = None
        self.taskmanager = None

    def set_datasource(self, ds):
        self._datasource = ds
        self.taskmanager = TaskManager(self._datasource, self)
    
    def get_datasource(self):
        return self._datasource

    datasource = property(get_datasource, set_datasource)

    def run_server(self, inputlist):
        self.datasource = mapreducer.Reader(FLAGS.reader)().Read(inputlist)
        print self.datasource
        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.bind(("", FLAGS.port))
        self.listen(1)
        try:
            asyncore.loop()
        except:
            asyncore.close_all()
            raise
        logging.info("Mapreduce done.")
        return self.taskmanager.results

    def handle_accept(self):
        pair = self.accept()
        if pair is None:
            pass
        conn, addr = pair
        logging.info("New client arrived at " + str(addr))
        sc = ServerChannel(conn, addr, self)

    def handle_close(self):
        self.close()


class ServerChannel(Protocol):
    def __init__(self, conn, addr, server):
        Protocol.__init__(self, conn)
        self.server = server
        self.addr = str(addr)
        self.start_auth()

    def handle_close(self):
        logging.info("Client %s disconnected" % (self.addr))
        self.close()

    def start_auth(self):
        self.send_challenge()

    def start_new_task(self):
        command, data = self.server.taskmanager.next_task(self)
        if command == None:
            return
        self.send_command(command, data)

    def map_done(self, command, data):
        self.server.taskmanager.map_done(data)
        self.start_new_task()

    def reduce_done(self, command, data):
        self.server.taskmanager.reduce_done(data)
        self.start_new_task()

    def process_command(self, command, data=None):
        handlers = {
            COMMAND.mapdone: self.map_done,
            COMMAND.reducedone: self.reduce_done,
            }
        try:
            handlers[command](command, data)
        except KeyError:
            Protocol.process_command(self, command, data)

    def post_auth_init(self):
        self.start_new_task()
    

class TaskManager(object):
    def __init__(self, datasource, server):
        self.datasource = datasource
        self.server = server
        self.state = TASK.START

    def next_task(self, channel):
        """Returns the next task to carry out
        """
        if self.state == TASK.START:
            logging.info("Start mapreduce")
            self.map_iter = iter(self.datasource)
            self.working_maps = set()
            self.map_results = {}
            logging.info("Start map phase")
            self.state = TASK.MAPPING
        
        if self.state == TASK.MAPPING:
            try:
                # get next map task
                map_key = self.map_iter.next()
                self.working_maps.add(map_key)
                return (COMMAND.map, (map_key, self.datasource[map_key]))
            except StopIteration:
                # if we finished sending out all map tasks, select one task
                # from the existing pools (in case some of the jobs died for
                # some reason). If all maps are done, we go on to reduce
                if len(self.working_maps) > 0:
                    key = random.choice(list(self.working_maps))
                    return (COMMAND.map, (key, self.datasource[key]))
                else:
                    logging.info("Map done. Start Reduce phase")
                    self.state = TASK.REDUCING
                    self.reduce_iter = self.map_results.iteritems()
                    self.working_reduces = set()
                    self.results = {}

        if self.state == TASK.REDUCING:
            try:
                reduce_item = self.reduce_iter.next()
                self.working_reduces.add(reduce_item[0])
                return (COMMAND.reduce, reduce_item)
            except StopIteration:
                if len(self.working_reduces) > 0:
                    key = random.choice(list(self.working_reduces))
                    return (COMMAND.reduce, (key, self.map_results[key]))
                else:
                    logging.info("Reduce phase done. Cleaning")
                    self.state = TASK.FINISHED
        if self.state == TASK.FINISHED:
            self.server.handle_close()
            return (COMMAND.disconnect, None)

    
    def map_done(self, data):
        # Don't use the results if they've already been counted
        if not data[0] in self.working_maps:
            return
        for (key, values) in data[1].iteritems():
            if key not in self.map_results:
                self.map_results[key] = []
            self.map_results[key].extend(values)
        self.working_maps.remove(data[0])
                                
    def reduce_done(self, data):
        # Don't use the results if they've already been counted
        if not data[0] in self.working_reduces:
            return
        self.results[data[0]] = data[1]
        self.working_reduces.remove(data[0])


def main(argv=None):
    if argv is None:
        argv = sys.argv
    try:
        # parse flags
        inputlist = FLAGS(argv)
    except gflags.FlagsError, e:
        print '%s\\nUsage: %s ARGS\\n%s' % (e, argv[0], FLAGS)
        sys.exit(1)
    if FLAGS.server:
        # server mode
        s = Server()
        result = s.run_server(inputlist)
        print result
    else:
        # client mode
        c = Client()
        c.run_client()
