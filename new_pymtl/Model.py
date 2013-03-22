#=========================================================================
# Model
#=========================================================================
#Base modeling components for constructing hardware description models.
#
#This module contains a collection of classes that can be used to construct MTL
#(pronounced metal) models. Once constructed, a MTL model can be leveraged by
#a number of tools for various purposes (simulation, translation into HDLs, etc).

from connection_graph import ConnectionSlice, Constant
from signals          import Port, InPort, OutPort, Wire

#from physical import PhysicalDimensions
#import PortBundle

import collections
import inspect
import ast
import copy

#-------------------------------------------------------------------------
# Logic Syntax Exception
#-------------------------------------------------------------------------
class LogicSyntaxError(Exception):
  pass

#-------------------------------------------------------------------------
# Model
#-------------------------------------------------------------------------
# User visible base class for hardware models.
#
# Provides utility classes for elaborating connectivity between components,
# giving instantiated subcomponents proper names, and building datastructures
# that can be leveraged by various MTL tools.
#
# Any user implemented model that wishes to make use of the various MTL tools
# should subclass this.
#
class Model(object):


  #-----------------------------------------------------------------------
  # Elaborate
  #-----------------------------------------------------------------------
  def elaborate(self):
    self.model_classes = set()
    self.recurse_elaborate(self, 'top')
    self.recurse_connections()
    # TODO: this only checks one instance of each model_class, it is
    #       theoretically possible that several instances of a given
    #       model_class could each have different types assigned to the
    #       same variable name...
    for c in self.model_classes:
      src = inspect.getsource( c.__class__ )
      tree = ast.parse( src )
      accesses = set()
      CheckSyntaxVisitor(accesses).visit(tree)
      for a in accesses:
        if a[0] in vars(c):
          ptr = c.__getattribute__( a[0] )
          # Check InPort vs OutPort?
          if   isinstance( ptr, (Port,Wire) ) and a[1] == 'wr_value':
            raise LogicSyntaxError("Writing .value in an @tick or @posedge_clk block!")
          elif isinstance( ptr, (Port,Wire) ) and a[1] == 'wr_next':
            raise LogicSyntaxError("Writing .next in an @combinational block!")
          elif isinstance( ptr, (Port,Wire) ) and a[1] == 'rd_next':
            raise LogicSyntaxError("Reading .next inside logic block not allowed!")
          elif isinstance( ptr, (Port,Wire) ) and a[1] == False:
            print "WARNING: reading from Port without .value.",
            print "Module: {0}  Port: {1}".format( c.class_name, ptr.name)

  #-----------------------------------------------------------------------
  # Recurse Elaborate
  #-----------------------------------------------------------------------
  def recurse_elaborate(self, target, iname):
    """Elaborate a MTL model (construct hierarchy, name modules, etc.).

    The elaborate() function must be called on an instantiated toplevel module
    before it is passed to any MTL tools!
    """
    # TODO: call elaborate() in the tools?
    # TODO: better way to set the name?
    self.model_classes.add( target )
    target.class_name = self.gen_class_name( target )
    target.parent = None
    target.name = iname
    target.clk   = InPort(1)
    target.reset = InPort(1)
    target._exe_seq_logic  = False
    target._exe_comb_logic = False
    target._line_trace_en  = False
    target._wires       = []
    target._ports       = []
    target._inports     = []
    target._outports    = []
    target._submodules  = []
    target._senses      = []
    if not hasattr( target, '_newsenses' ):
      target._newsenses   = collections.defaultdict( list )
    target._localparams = set()
    target._connections = set()
    target._tempwires   = {}
    target._temparrays  = []
    target._tempregs    = []
    target._loopvars    = []
    #target._dim = PhysicalDimensions()
    # TODO: do all ports first?
    # Get the names of all ports and submodules
    for name, obj in target.__dict__.items():
      if not name.startswith('_'):
        self.check_type(target, name, obj)
    # Infer the sensitivity list for combinational blocks
    src = inspect.getsource( target.__class__ )
    tree = ast.parse( src )
    SensitivityListVisitor( target ).visit(tree)

  #-----------------------------------------------------------------------
  # Check Type
  #-----------------------------------------------------------------------
  def check_type(self, target, name, obj):
    """Utility method to specialize elaboration actions based on object type."""
    # If object is a wire, add it to our sensitivity list
    # TODO: Wires are currently subclasses of Ports, so this check must be
    #       first.  Fix?
    if isinstance(obj, Wire):
      obj.name = name
      obj.parent = target
      target._wires += [obj]
      # TODO: also add ImplicitWires to sensitivity list?
      target._senses += [obj]
    # If object is a port, add it to our ports list
    elif isinstance(obj, InPort):
      obj.name = name
      obj.parent = target
      target._ports += [obj]
      target._inports += [obj]
      if obj.name != 'clk':
        target._senses += [obj]
    elif isinstance(obj, OutPort):
      obj.name = name
      obj.parent = target
      target._ports += [obj]
      target._outports += [obj]
    #elif isinstance(obj, PortBundle.PortBundle):
    #  for port_name, obj in obj.__dict__.items():
    #    self.check_type(target, name+'_M_'+port_name, obj)
    # If object is a submodule, add it to our submodules list and recursively
    # call elaborate() on it
    elif isinstance(obj, Model):
      self.recurse_elaborate( obj, name )
      obj.parent = target
      connect( obj.clk, obj.parent.clk )
      connect( obj.reset, obj.parent.reset )
      target._submodules += [obj]
      # Add all the output ports of the child module to our sensitivity list
      target._senses += [ x for x in obj._ports if isinstance(x, OutPort) ]
    # We've found a constant assigned to a global variable.
    # TODO: add support for floats?
    elif isinstance(obj, int):
      target._localparams.add( (name, obj) )
    # If the object is a list, iterate through each item in the list and
    # recursively call the check_type() utility function
    elif isinstance(obj, list):
      # TODO: fix to handle Wire lists properly
      if obj and isinstance(obj[0], Port):
        target._temparrays.append( name )
      for i, item in enumerate(obj):
        if isinstance( item, Wire ):
          item_name = "%s[%d]" % (name, i)
        else:
          item_name = "%sIDX%d" % (name, i)
        self.check_type(target, item_name, item)

  #-----------------------------------------------------------------------
  # Generate Class Name
  #-----------------------------------------------------------------------
  def gen_class_name(self, model):
    name = model.__class__.__name__
    try:
      for arg_name, arg_val in model._args.items():
        name += "_{}_{}".format( arg_name, arg_val )
      return name
    except AttributeError:
      return name

  #-----------------------------------------------------------------------
  # Recurse Connections
  #-----------------------------------------------------------------------
  def recurse_connections(self):

    for port in self._ports:

      for c in port.connections:
        # Set the directionality of this connection
        self.set_edge_direction( c )
        # Classify as either an internal or external connection
        if c.is_internal( port ):
          port.int_connections += [c]
          self._connections.add( c )
        else:
          port.ext_connections += [c]
          self.parent._connections.add( c )
        # TODO: make connect a self.connect() method instead, add to
        #       self._connections then instead?

    # Recursively enter submodules

    for submodule in self._submodules:
      submodule.recurse_connections()

  #-----------------------------------------------------------------------
  # Set Edge Direction
  #-----------------------------------------------------------------------
  def set_edge_direction(self, edge):
    a = edge.src_node
    b = edge.dest_node

    # Constants should always be the source node
    if isinstance( b, Constant ):
      edge.swap_direction()

    # Model connecting own InPort to own OutPort
    elif ( a.parent == b.parent and
           isinstance( a, OutPort ) and isinstance( b, InPort  )):
      edge.swap_direction()

    # Model InPort connected to InPort of a submodule
    elif ( a.parent in b.parent._submodules and
           isinstance( a, InPort  ) and isinstance( b, InPort  )):
      edge.swap_direction()

    # Model OutPort connected to OutPort of a submodule
    elif ( b.parent in a.parent._submodules and
           isinstance( a, OutPort ) and isinstance( b, OutPort )):
      edge.swap_direction()

    # Wire connected to InPort
    elif ( a.parent == b.parent and
           isinstance( a, Wire ) and isinstance( b, InPort )):
      edge.swap_direction()

    # Wire connected to OutPort
    elif ( a.parent == b.parent and
           isinstance( a, OutPort ) and isinstance( b, Wire )):
      edge.swap_direction()

    # Wire connected to InPort of a submodule
    elif ( a.parent in b.parent._submodules and
           isinstance( a, InPort  ) and isinstance( b, Wire )):
      edge.swap_direction()

    # Wire connected to OutPort of a submodule
    elif ( b.parent in a.parent._submodules and
           isinstance( a, Wire ) and isinstance( b, OutPort )):
      edge.swap_direction()

    # Chaining submodules together (OutPort of one to InPort of another)
    elif ( a.parent != b.parent and a.parent.parent == b.parent.parent and
           isinstance( a, InPort  ) and isinstance( b, OutPort )):
      edge.swap_direction()

  #-----------------------------------------------------------------------
  # Set Attr
  #-----------------------------------------------------------------------

  #def __setattr__( self, name, value ):
  #  if name in self.__dict__ and isinstance( self.__dict__[ name ], Bits):
  #    self.__dict__[ name ].uint = value
  #  else:
  #    self.__dict__[ name ] = value

  #-----------------------------------------------------------------------
  # Getters
  #-----------------------------------------------------------------------

  def get_inports( self ):
    return self._inports

  def get_outports( self ):
    return self._outports

  def get_ports( self ):
    return self._inports + self._outports

  def get_wires( self ):
    return self._wires

  def get_submodules( self ):
    return self._submodules

  def get_sensitivity_list( self ):
    return self._senses

  def get_localparams( self ):
    return self._localparams

  def get_connections( self ):
    return self._connections

  #-----------------------------------------------------------------------
  # Register Combinational
  #-----------------------------------------------------------------------
  def register_combinational( self, func_name, sensitivity_list ):
    self._newsenses = collections.defaultdict( list )
    self._newsenses[ func_name ] = sensitivity_list

  #-----------------------------------------------------------------------
  # Dump Physical Design
  #-----------------------------------------------------------------------
  #def dump_physical_design(self, prefix=''):
  #  if prefix:
  #    fullname = prefix + '.' + self.name
  #  else:
  #    fullname = self.name

  #  print fullname, self._dim.get_rectangle()
  #  for rect in self.get_submodules():
  #    rect.dump_physical_design( fullname )

  #-----------------------------------------------------------------------
  # Is Elaborated
  #-----------------------------------------------------------------------
  def is_elaborated(self):
    return hasattr(self, 'class_name')

  #-----------------------------------------------------------------------
  # Line Trace
  #-----------------------------------------------------------------------
  # Having a default line trace makes it easier to just always enable
  # line tracing in the test harness. -cbatten
  def line_trace(self):
    return ""

#------------------------------------------------------------------------
# Connect Functions
#------------------------------------------------------------------------

def connect_ports( port_A, port_B):
  if   isinstance(port_B, int):
    port_A.connect( Constant(port_B, port_A.width) )
  elif isinstance(port_A, ConnectionSlice):
    port_B.connect( port_A )
  else:
    port_A.connect( port_B )

def connect_dict( connections ):
 for left_port,right_port in connections.iteritems():
   connect( left_port, right_port )

def connect( left, right=None ):
 if type(left) == dict:
   connect_dict( left )
 else:
   connect_ports( left, right )

import ast, _ast
#------------------------------------------------------------------------
# Check Syntax Visitor
#------------------------------------------------------------------------
# Hidden class checking the validity of port and wire accesses.
#
# In order to translate synchronous @posedge_clk blocks into Verilog, we need
# to declare certain wires as registers.  This visitor looks for all ports
# written in @posedge_clk blocks so they can be declared as reg types.
class CheckSyntaxVisitor(ast.NodeVisitor):

  def __init__(self, accesses):
    """Construct a new CheckSyntaxVisitor."""
    self.accesses = accesses
    self.decorator = None

  def visit_FunctionDef(self, node):
    """Visit all functions, but only parse those with special decorators."""
    if not node.decorator_list:
      return
    elif node.decorator_list[0].id in ['tick','posedge_clk', 'combinational']:
      # Visit each line in the function, translate one at a time.
      self.decorator = node.decorator_list[0].id
      for x in node.body:
        self.visit(x)
      self.decorator = None

  def visit_Attribute(self, node):
    """Visit all attributes, convert into Verilog variables."""
    #target_name, debug = self.get_target_name(node)
    if self.decorator:
      target_name, debug = self.get_target_name(node)
      if  self.decorator in ['tick', 'posedge_clk'] and debug == 'value':
        self.accesses.add( (target_name, 'rd_'+debug, node.lineno) )
      elif self.decorator == 'combinational' and debug == 'next':
        self.accesses.add( (target_name, 'rd_'+debug, node.lineno) )
      # TODO: why are we checking syntax of non-annotated blocks...
      else:
        self.accesses.add( (target_name, debug, node.lineno) )

  def visit_Assign(self, node):
    """Visit all assigns, searches for synchronous (registered) stores."""
    # Only works for one lefthand target
    assert len(node.targets) == 1
    target = node.targets[0]
    target_name, debug = self.get_target_name(target)
    # We are writing value inside of a posedge_clk, raise exception
    if   self.decorator in ['tick', 'posedge_clk'] and debug == 'value':
      self.accesses.add( (target_name, 'wr_'+debug, node.lineno) )
    # We are writing next inside of a combinational, raise exception
    elif self.decorator == 'combinational' and debug == 'next':
      self.accesses.add( (target_name, 'wr_'+debug, node.lineno) )
    self.visit( node.value )

  def get_target_name(self, node):
    # Is this an attribute? Follow it until we find a Name.
    name = []
    while isinstance(node, _ast.Attribute):
      name += [node.attr]
      node = node.value
    # Subscript, do nothing
    if isinstance(node, _ast.Subscript):
      return None, False
    # We've found the Name.
    assert isinstance(node, _ast.Name)
    name += [node.id]
    # If the target does not access .value or .next, tell the code to ignore it.
    if name[0] in ['value', 'next']:
      return '.'.join( name[::-1][1:-1] ), name[0]
    else:
      return name[0], False

#------------------------------------------------------------------------
# Sensitivity List Visitor
#------------------------------------------------------------------------
# TODO: add to simulator instead of model?
class SensitivityListVisitor(ast.NodeVisitor):
  """Hidden class detecting the sensitivity list of @combinational blocks"""

  #----------------------------------------------------------------------
  # Constructor
  #----------------------------------------------------------------------
  def __init__(self, model ):
    """Construct a new CheckSyntaxVisitor."""
    self.func_name = None
    self.model     = model

  #----------------------------------------------------------------------
  # Visit Functions
  #----------------------------------------------------------------------
  def visit_FunctionDef(self, node):
    """Visit all functions, but only parse those with special decorators."""
    if not node.decorator_list:
      return
    elif node.decorator_list[0].id == 'combinational':
      # Visit each line in the function, translate one at a time.
      self.func_name = node.name
      for x in node.body:
        self.visit(x)
      self.func_name = None

  #----------------------------------------------------------------------
  # Visit Attributes
  #----------------------------------------------------------------------
  def visit_Attribute(self, node):
    """Visit all attributes, convert into Verilog variables."""
    if not self.func_name:
      return

    # Hacky way to get subscripts of stores to port lists
    signal_ptr = self.get_target( node )

    if isinstance( node.ctx, _ast.Load ):

      if   isinstance( signal_ptr, list ):
        # TODO: this will allow duplicate entries to be in the _newsenses
        #       list, do we need to fix this?
        self.model._newsenses[ self.func_name ].extend( signal_ptr )
      elif signal_ptr:
        if signal_ptr not in self.model._newsenses[ self.func_name ]:
          self.model._newsenses[ self.func_name ] += [ signal_ptr ]
      #if target_name in vars( self.model ):
      #  signal_ptr = self.model.__getattribute__( target_name )
      #  self.model._newsenses[ self.func_name ] += [signal_ptr]
      #else:
      #  print "BAD", node.attr, node.value, target_name


  #----------------------------------------------------------------------
  # Create a List to Target
  #----------------------------------------------------------------------
  def get_target(self, node):

    # Is this a number/constant? Return it.
    if isinstance(node, _ast.Num):
      raise Exception("Ran into a number/constant!")
      return node.n, True

    # Is this an attribute? Follow it until we find a Name.
    name = []
    while isinstance(node, (_ast.Attribute, _ast.Subscript)):
      if   isinstance(node, _ast.Attribute):
        name += [node.attr]
        node = node.value
      elif isinstance(node, _ast.Subscript):
        # Visit the index, if its a variable want to add to sensitivity
        self.visit( node.slice )
        # TODO: assumes this is an integer, not a range
        if (isinstance( node.slice, _ast.Index) and
            isinstance( node.slice.value, _ast.Num )):
          name += [ node.slice.value.n ]
          node = node.value
        else:
          name += [ '?' ]
          node = node.value

    # We've found the Name.
    assert isinstance(node, _ast.Name)
    name += [node.id]

    # If the target does not access .value or .next, tell the code to ignore it.
    if   name[0] in ['value', 'next']:
      return self.get_target_ptr( name[::-1][1:-1] )
    # Special case Bits methods
    elif name[0] in ['uint', 'int', 'sext', 'zext']:
      return self.get_target_ptr( name[::-1][1:-2] )
    else:
      #print "BAD",  '.'.join( name[::-1] )
      return None

  #----------------------------------------------------------------------
  # Get Pointer to Target Signal
  #----------------------------------------------------------------------
  def get_target_ptr(self, target_list, obj = None):
    if not obj:
      obj = self.model
    for i, attr in enumerate(target_list):
      # This is an index into an array of objects, get there right object
      if isinstance(attr, int) and isinstance( obj, list ):
        obj = obj[ attr ]
      # This is an index into an array of objects, but we can't determine
      # which object.  Return ALL the objects in the array.
      elif attr == '?' and isinstance( obj, list ):
        x = []
        for subobj in obj:
          x.append( self.get_target_ptr( target_list[i+1:], subobj ) )
        return x
      # This is an index into a slice! Just return the object.
      elif attr == '?' or isinstance(attr, int):
        return obj
      # This is an attribute, get the object with this attribute name
      else:
        # Handling for BitStructs.  Python checks __getattribute__
        # first when trying to find a class member.  After that, it
        # checks __getattr__,  a method implemented by developers to
        # provide alternate checks for members.  In our case, we modify
        # Ports to use __getattr__ to provide access to BitStructs fields
        # assoiated with the port when using the syntax
        # <portname>.<fieldname>.  Other components do NOT implement
        # __getattr__, so check __getattribute__ first, then check
        # __getattr__ as a sanity check to ensure the field name is valid,
        # although we simply discard the returned object.
        try:
          obj = obj.__getattribute__( attr )
        except AttributeError:
          discard_me = obj.__getattr__( attr )

    return obj

#------------------------------------------------------------------------
# Decorators
#------------------------------------------------------------------------
# Normally a decorator returns a wrapped function, but here we return
# func unmodified.  We only use the decorator as a flag for the ast
# parsers.

def combinational(func):
  return func

def posedge_clk(func):
  return func

def tick(func):
  return func

# import functools
import inspect
#from itertools import chain
def capture_args(fn):
  "Returns a traced version of the input function."

  #@functools.wraps(fn)
  def wrapped(*v, **k):

    # get the names of the functions arguments
    argspec = inspect.getargspec( fn )

    # the self pointer is always the first positional arg
    self = v[0]

    # create an argument dictionary
    args = collections.OrderedDict()
    # add all the positional arguments
    for i in range(1, len(v)):
      if isinstance( v[i], int ):
        value = v[i]
      else:
        #raise Exception("Untranslatable param type!")
        # WARNING: taking abs() of hash, increases chance of collision?
        value = hex( abs( hash( v[i] )) )
      key = argspec.args[ i ]
      args[ key ] = value
    # then add all the named arguments
    for key, val in k.items():
      if isinstance( val, int ):
        value = val
      else:
        # WARNING: taking abs() of hash, increases chance of collision?
        value = hex( abs( hash( val )) )
      args[key] = value

    # add the arguments and their values to the object so it can be
    # used during static elaboration to create the name
    self._args = args

    return_val  = fn(*v, **k)
    return return_val

  return wrapped

