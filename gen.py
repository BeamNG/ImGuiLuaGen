#!/usr/bin/python3
# vim: set fileencoding=utf-8
#
# MIT License
# Copyright 2019-2023 BeamNG GmbH
#
#Permission is hereby granted, free of charge, to any person obtaining a copy of
#this software and associated documentation files (the "Software"), to deal in
#the Software without restriction, including without limitation the rights to
#use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
#of the Software, and to permit persons to whom the Software is furnished to do
#so, subject to the following conditions:
#
#The above copyright notice and this permission notice shall be included in all
#copies or substantial portions of the Software.
#
#THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
#AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
#LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
#OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
#SOFTWARE.
#
#
# This is a python3 script that generates LuaJIT FFI bindings and lua wrappers
# Used in BeamNG to generate the ImGui wrapper
#
# 1) install all the dependencies:
#    * on Ubuntu 20.04:
#        apt install python3-pip clang libclang-dev libclang-6.0-dev
#        pip3 install clang
#    * on Windows 10:
#        ensure python3 and pip are installed and usable from command line
#        pip3 install clang
#        install LLVM to the default install path, using a prebuilt *win64.exe installer from: https://github.com/llvm/llvm-project/releases
# 2) run it like this:
#   python gen.py imgui/imgui.h
#
# About the 'design' of this file: It is kept as simple (and so hacky partly) as possible.
# Clang does the most work, we just need to make sense of the result here.
# I did not add an abstraction layer inbetween te AST walker and generators as i wanted to keep it simple.
# It has very specific behavior to fullfil it's job as imgui binding generator.
#
#  - Thomas Fischer <tfischer@beamng.gmbh> 06 Jan 2019
#  - Ludger Meyer-Wuelfing <lmeyerwuelfing@beamng.gmbh>
#  - Bruno Gonzalez Campo <bgonzalez@beamng.gmbh>
#
# TODO:
#  - fix variadic function wrapper arguments for function call on lua side (instead of 'C.imgui_TreeNode(str_id, fmt)' -> C.imgui_TreeNode(str_id, fmt, ...))
#
# CHANGES
#  - 19th of Apr 2022: windows port
#  - 8th of Oct 2020: removed context hacks from the generator
#
import sys
import os
import clang.cindex
from clang.cindex import CursorKind as CK
from clang.cindex import TokenKind as TK
from clang.cindex import TypeKind as TyK
import datetime
import pprint

# TODO: convtert to USR*
skip_names = [
  'SetAllocatorFunctions',
  'MemAlloc',
  'MemFree',
  'LoadIniSettingsFromDisk',
  'LoadIniSettingsFromMemory',
  'SaveIniSettingsToDisk',
  'SaveIniSettingsToMemory',
  'ImGuiOnceUponAFrame',
  'ImNewDummy',
  'ImDrawChannel',
  'ImFontGlyphRangesBuilder_BuildRanges'
]

skip_usrs = [
  'c:@N@ImGui@F@SetNextWindowClass#*$@S@ImGuiContext#*1$@S@ImGuiWindowClass#', # duplicate

  # we have custom replacements:
  'c:@N@ImGui@F@CreateContext#*$@S@ImFontAtlas#',
  'c:@N@ImGui@F@DestroyContext#*$@S@ImGuiContext#',

  'c:@S@ImVec2',
  'c:@S@ImVec4',

  # invalid code generated:
  'c:@N@ImGui@F@LogText#*$@S@ImGuiContext#*1C.#',
  'c:@S@ImGuiTextBuffer@F@appendf#*1C.#',
  'c:@S@ImFontAtlas@F@GetCustomRectByIndex#I#1', # Lua does not know about the nested datatype
  'c:@S@ImFontAtlas@F@CalcCustomRectUV#*1$@S@ImFontAtlas@S@CustomRect#*$@S@ImVec2#S2_#', # Lua does not know about the nested datatype
  'c:@S@ImFontGlyphRangesBuilder@F@BuildRanges#*$@S@ImVector>#s#' # Template parameter in function
]

skip_constructors = [
  'ImGuiTextFilter',
  'ImDrawList'
]

debug = False

# do not change below

fileCache = {}

# dumps a cursor to the screen, recursively
def dumpCursor(c, level):
  print(' ' * level, str(c.kind)[str(c.kind).index('.')+1:], c.type.spelling, c.spelling)
  print(' ' * level, '  ', getContent(c, True))
  for cn in c.get_children():
    dumpCursor(cn, level + 1)

# gets the content for that cursor from the file
def getContent(c, shortOnly):
  global fileCache
  filename = str(c.extent.start.file)
  if filename == 'None':
    return ''
  if not filename in fileCache:
    with open(filename, 'r') as f:
      fileCache[filename] = f.readlines()

  fileContent = fileCache[filename]
  # too long?
  if shortOnly and c.extent.start.line != c.extent.end.line:
    return '<>'
  # fiddle out the content
  res = ''
  for i in range(c.extent.start.line - 1, c.extent.end.line):
    if i == c.extent.start.line - 1 and i == c.extent.end.line - 1:
      res += fileContent[i][c.extent.start.column-1:c.extent.end.column-1]
    elif i == c.extent.start.line - 1:
      res += fileContent[i][c.extent.start.column-1:]
    elif i == c.extent.end.line - 1:
      res += fileContent[i][:c.extent.end.column-1]
    else:
      res += fileContent[i]
  return res.strip()

# this function prevents lua parameters being lua keywords
def luaParameterSpelling(c, addSimpleType):
  reserved_lua_keywords = { 'and':1, 'break':1, 'do':1, 'else':1, 'elseif':1, 'end':1,
    'false':1, 'for':1, 'function':1, 'if':1, 'in':1,
    'local':1, 'nil':1, 'not':1, 'or':1, 'repeat':1,
    'return':1, 'then':1, 'true':1, 'until':1, 'while':1
  }
  parName = c.spelling
  if parName in reserved_lua_keywords:
    return '_' + parName

  # add the type to the var name as helper for lua users
  if addSimpleType:
    simpletype = c.type.spelling
    #if c.type.kind == TyK.TYPEDEF:
    #  simpletype = c.type.get_canonical().spelling
    if simpletype.find('(*)') >= 0:
      simpletype = 'functionPtr'
    else:
      if simpletype.find('[') >= 0:
        simpletype = simpletype[:simpletype.find('[')].strip() + 'Ptr'
      simpletype = simpletype.replace('const ', '')
      simpletype = simpletype.replace('unsigned ', '')
      simpletype = simpletype.replace(' ', '')
      simpletype = simpletype.replace('*', '')
      simpletype = simpletype.replace('&', '')
      if simpletype == 'char': simpletype = 'string'
    return simpletype + '_' + parName
  else:
    return parName

# fixes up some variable naming and type things
def getCVarStr(c, addSimpleType):
  # the array is with the type, should be woth the varname instead in C
  res = ''
  if c.type.spelling.find('[') >= 0:
    typeWithOutArr = c.type.spelling[:c.type.spelling.find('[')].strip()
    arrOnly = c.type.spelling[c.type.spelling.find('['):]
    res = typeWithOutArr + ' ' + c.spelling + arrOnly
  elif c.type.spelling.find('<') >= 0:
    res = c.type.spelling[:c.type.spelling.find('<')].strip() + ' ' + luaParameterSpelling(c, addSimpleType)
  elif c.type.spelling.find('(*)') >= 0:
    # a function pointer, add the name back into it properly...
    res = c.type.spelling
    res = res.replace('(*)', '(*' + luaParameterSpelling(c, addSimpleType) + ')')
  else:
    res = c.type.spelling + ' ' + luaParameterSpelling(c, addSimpleType)

  # remove some space ;)
  res = res.replace(' &', '*')
  res = res.replace(' *', '*')
  return res

def stripSizeOf(s):
  i = 0
  for c in s:
    if c == '(':
      s = s[i+1:]
      break
    i += 1
  i = 0
  for c in s:
    if c == ')':
      s = s[:i]
      break
    i += 1
  return s

# converts a c value to a lua value - used for optional arguments
def luaifyValueWithType(p, s):
  t = p.type
  k = t.kind
  #print(" === luaifyValue ")
  #print(" k = "+ str(k))
  #print(" s = "+ str(s))
  if k == TyK.BOOL:
    return s
  elif k == TyK.INT or k== TyK.UINT:
    s.replace('+', '')
    if s.startswith('Im'):
      s = 'M.' + s
    if s.startswith('sizeof'):
      s = 'ffi.sizeof(\'' + stripSizeOf(s) + '\')'
    return s
  elif k == TyK.FLOAT or k == TyK.DOUBLE:
    return s.replace('+', '').replace('f', '').replace('.0', '')
  elif (k == TyK.POINTER or k == TyK.TYPEDEF) and (s == 'nullptr' or s == 'NULL'):
    return 'nil' # '0'
  # elif (k == TyK.POINTER or k == TyK.TYPEDEF) and s == 'NULL':
    # return 'nil'
  elif k == TyK.POINTER and s[0] == '"':
    pass
  elif k == TyK.LVALUEREFERENCE or k == TyK.RECORD or k == TyK.ENUM: # and s.find('ImVec') == 0:
    return 'M.' + s
  elif k == TyK.TYPEDEF:
    # Need to dereference typedefs to ensure the correct lua code gets generated
    underlying_type = t.get_canonical()
    p.type = underlying_type
    return luaifyValueWithType(p, s)
  else:
    print("unknown value type: ", k, s, ' ### parent = ', t.spelling, ' ', p.spelling)
  return s

# converts a c value to a lua value - used for optional arguments
def luaifyValue(cParent, s):
  return luaifyValueWithType(cParent, s)

def getLuaFunctionOptionalParams(c):
  parameter_opt = None
  token = list(c.get_tokens())
  for p in c.get_arguments():
    # we want the optional argument, so we need to wak all tokens completely until the next valid comma to catch everything
    for i in range(0, len(token)):
      if token[i].kind == TK.IDENTIFIER and token[i].spelling == p.spelling and i < len(token) - 3:
        i+=1
        if token[i].kind == TK.PUNCTUATION and token[i].spelling == '=' and i < len(token) - 2:
          i+=1
          braceStack = 0
          optArg = ''
          while i < len(token) - 1:
            #print(braceStack, token[i].spelling)
            if token[i].kind == TK.PUNCTUATION and token[i].spelling == '(':
              braceStack += 1
            elif token[i].kind == TK.PUNCTUATION and token[i].spelling == ')':
              if braceStack <= 0:
                break
              braceStack -= 1
            elif token[i].kind == TK.PUNCTUATION and token[i].spelling == ',' and braceStack <= 0:
              break
            optArg += token[i].spelling
            #print("TOKENTRACE", i, token[i].kind, token[i].spelling)
            i+=1

          if parameter_opt is None: parameter_opt = {}

          parameter_opt[luaParameterSpelling(p, True)] = luaifyValue(p, optArg)

        # only find the first matching parameter, not every random token with the same name after as well ...
        break
  return parameter_opt

###############################################################################

# conventions:
#  *C VM*   = C code for inside the lua VM, as in the FFI defitions
#  *C Host* = Code for the Lua Host, that exports the FFI bindings
#  *Lua VM* = Lua code for inside the VM that does helper things like optional args

class BindingGenerator:
  def __init__(self, debug):
    self.functionRenames = {}
    self.debug = debug

  ## structs
  def _generateCVMStruct(self, c, level):
    functionCache = ''
    res = ''
    if c.kind == CK.STRUCT_DECL:
      if level == 0:
        res += '  ' * (level - 1) + 'typedef struct ' + c.spelling + " {\n"
      else:
        res += '  ' * (level - 1) + 'struct ' + c.spelling + " {\n"
    elif c.kind == CK.UNION_DECL:
      res += '\n' + '  ' * (level - 1) + 'union {\n'

    for ch in c.get_children():
      if ch.kind == CK.FIELD_DECL:
        # simplify function pointers
        if ch.type.spelling.find('(') >= 0:
          res += '  ' * (level + 1) + 'void* ' + ch.spelling + "; // complex callback: " + ch.type.spelling + ' - ' + self.getCursorDebug(ch, '') + '\n'
        else:
          res += '  ' * (level + 1) + getCVarStr(ch, False) + ";" + self.getCursorDebug(ch, '   // ') + '\n'
      elif ch.kind == CK.CONSTRUCTOR and level == 0:
        #res += '  // ' + self._generateCVMFunction(ch, '', None)
        functionCache += '// ' + self._generateCVMFunction(ch, 'imgui_', None)

      elif ch.kind == CK.STRUCT_DECL or ch.kind == CK.UNION_DECL:
        res += '  ' * (level + 1) + self.getCursorDebug(ch, ' // ') + '\n'
        res += self._generateCVMStruct(ch, level + 1)

      elif (ch.kind == CK.FUNCTION_DECL or ch.kind == CK.CXX_METHOD) and ch.spelling.find('operator') == -1 and level == 0:
        #res += '  // ' + self._generateCVMFunction(ch, '', None)
        if ch.get_usr() in skip_usrs or ch.spelling in skip_names:
          pass
        else:
          functionCache += self._generateCVMFunction(ch, 'imgui_' + c.spelling + '_', c.spelling + '* ' + c.spelling + '_ctx')

    if c.kind == CK.STRUCT_DECL:
      if level == 0:
        res += '  ' * level + '} ' + c.spelling + ';\n'
      else:
        res += '  ' * level + '};\n'
    elif c.kind == CK.UNION_DECL:
      res += '  ' * level + '};\n'

    res += functionCache

    return res

  ## struct member functions
  def _generateLVMStruct(self, c):
    if debug:
      res = '--=== struct ' + c.spelling + ' === ' + c.get_usr() + '\n'
    else:
      res = '--=== struct ' + c.spelling + ' ===\n'
    for ch in c.get_children():
      if ch.get_usr() in skip_usrs or ch.spelling in skip_names or ch.kind == CK.CLASS_TEMPLATE or ch.kind == CK.FUNCTION_TEMPLATE:
        continue

      # print(c.spelling,ch.kind , ch.spelling)
      if (ch.kind == CK.FUNCTION_DECL or ch.kind == CK.CXX_METHOD) and ch.spelling.find('operator') == -1:
        res += self._generateLuaVMFunction(ch, c.spelling + '_', 'imgui_' + c.spelling + '_', c.spelling + '_ctx')
      elif ch.kind == CK.CONSTRUCTOR:
        if ch.spelling in skip_constructors:
          continue
        else:
          res += self._generateLuaConstructor(ch)
        # pass
    res += '--===\n'
    return res

  def _generateCHostStruct(self, c):
    res = ''
    for ch in c.get_children():
      if ch.get_usr() in skip_usrs or ch.spelling in skip_names or ch.kind == CK.CLASS_TEMPLATE or ch.kind == CK.FUNCTION_TEMPLATE:
        continue
      # print(c.spelling,ch.kind , ch.spelling)
      if (ch.kind == CK.FUNCTION_DECL or ch.kind == CK.CXX_METHOD) and ch.spelling.find('operator') == -1:
        res += self._generateCHostFunction(ch, 'imgui_' + c.spelling + '_', c.spelling + '_ctx->', c.spelling + '_ctx', c.type.spelling)
    return res

  ## LVM Constructors
  def _generateLuaConstructor(self, c):
    signature, resStr, parameter_names, isVariadic, parameter_deref = self.getCFunctionSignature(c, '', None, False)
    i = 0
    for param in parameter_names:
      if param[0] == '_':
        parameter_names[i] = param[1:]
        i += 1
    #func = '-- ' + c.spelling + ' Constructor - ' + self.getCursorDebug(c, '') + '\n'
    func = 'function M.' + c.spelling + '(' + ', '.join(parameter_names) + ')'
    funcPtr = 'function M.' + c.spelling + 'Ptr(' + ', '.join(parameter_names) + ')'
    if len(parameter_names) > 0:
      func += '\n  local res = ffi.new("' + c.spelling + '")\n'
      for param in parameter_names:
        func +=  '  res.' + param + ' = ' + param + '\n'
      func += '  return res\n'
    else:
      func += ' return ffi.new("' + c.spelling + '") '
      funcPtr += ' return ffi.new("' + c.spelling + '[1]") '

    func += 'end\n'
    funcPtr += 'end\n'
    return func + funcPtr


  ## functions
  def _generateCVMFunction(self, c, prefix, firstArg):
    signature, resStr, parameter_names, isVariadic, parameter_deref = self.getCFunctionSignature(c, prefix, firstArg, False)
    return signature + ';' + self.getCursorDebug(c, '   // ') + '\n'

  def _generateCHostFunction(self, c, prefix, cNamespace, firstArgName, firstArgType):
    firstArg = None
    functionAppendix = ''
    if firstArgName and firstArgType:
      firstArg = firstArgType + '* ' + firstArgName
    signature, resStr, parameter_names, isVariadic, parameter_deref = self.getCFunctionSignature(c, prefix, firstArg, True)
    res = ''
    if self.debug:
      res += '\n' + self.getCursorDebug(c, '// ')+ '\n'

    res += 'FFI_EXPORT ' + signature + ' {\n'

    if isVariadic:
      functionAppendix = 'V'
      parameter_names.append('args')
      parameter_deref.append(False)
      res += '  va_list args;\n'
      res += '  va_start(args, fmt);\n'

    # TODO: FIXME: Disabled, as return needs a value, like false
    #if firstArgName:
    #  res += '  assert(' + firstArgName + ');\n'
    #  res += '  if (!' + firstArgName + ') return;\n'

    # built the parameter string
    paramArr = []
    for i in range(0, len(parameter_names)):
      if parameter_deref[i]:
        paramArr.append('*' + parameter_names[i])
      else:
        paramArr.append(parameter_names[i])
    paramStr = ', '.join(paramArr)

    rt = c.result_type
    if c.result_type.kind == TyK.TYPEDEF:
      rt = c.result_type.get_canonical()
    if rt.spelling == 'ImVec2':
      res += '  const ImVec2& res_cxx = ' + cNamespace + c.spelling + functionAppendix + '(' + paramStr + ');\n'
      res += '  ImVec2_C res_c = {res_cxx.x, res_cxx.y};\n'
      res += '  return res_c;\n'
    elif rt.spelling == 'ImVec4' or rt.spelling == 'ImColor':
      res += '  const ImVec4& res_cxx = ' + cNamespace + c.spelling + functionAppendix + '(' + paramStr + ');\n'
      res += '  ImVec4_C res_c = {res_cxx.x, res_cxx.y, res_cxx.z, res_cxx.w};\n'
      res += '  return res_c;\n'
    else:
      res += '  ' + resStr + cNamespace + c.spelling + functionAppendix + '(' + paramStr + ');\n'

    if isVariadic:
      res += '  va_end(args);\n'

    res += '}\n\n'
    return res

  def _generateLuaVMFunction(self, c, prefixLua, prefixC, firstArg):
    signature, resStr, parameter_names, isVariadic, parameter_deref = self.getCFunctionSignature(c, 'imgui_', None, False)
    parameters = []
    parameter_opt = getLuaFunctionOptionalParams(c)
    parameter_PtrChecks = {}
    for p in c.get_arguments():
      if p.spelling != 'ctx':
        parameters.append(luaParameterSpelling(p, True))
        if p.type.spelling.find('*') != -1:
          parameter_PtrChecks[luaParameterSpelling(p, True)] = p.type.spelling
    # ok, build the resulting lua code now ...
    multiLineFunction = False
    if firstArg:
      parameters.insert(0, firstArg)
    if isVariadic:
      parameters.append('...')
    res = ''
    if self.debug:
      res += '\n' + self.getCursorDebug(c, '-- ')+ '\n'
      multiLineFunction = True
    res += 'function M.' + prefixLua + self.getFunctionName(c) + '(' + ', '.join(parameters) + ') '
    if parameter_opt:
      multiLineFunction = True
      res += '\n'
      for k,v in parameter_opt.items():
        if v == 'nil':
          res += '  -- ' + k + ' is optional and can be nil\n'
        else:
          res += '  if '+ k + ' == nil then ' + k + ' = ' + v + ' end\n'

    if len(parameter_PtrChecks) > 0:
      if not multiLineFunction:
        res += '\n'
      multiLineFunction = True
      for k,v in parameter_PtrChecks.items():
        if parameter_opt and k in parameter_opt:
          continue
        res += '  if '+ k + ' == nil then log("E", "", "Parameter \'' + k + '\' of function \'' + self.getFunctionName(c) + '\' cannot be nil, as the c type is \'' + v + '\'") ; return end\n'

    if debug:
      res += '\n'
      parameters2 = []
      for p in parameters:
        if p == '...': p = '{...}'
        parameters2.append('" .. dumps(' + p + ') .. "')
      res += '  print("*** calling FFI: ' + prefixC + self.getFunctionName(c) + '(' + (', '.join(parameters2)) + ')")\n'
      # uncomment below for more debug:
      #res += '  print("*** stacktrace: " .. debug.tracesimple())\n'

    if multiLineFunction:
      res += '  '

    if c.result_type.spelling != 'void':
      res += 'return '
    res += 'C.' + prefixC + self.getFunctionName(c) + '(' + ', '.join(parameters) + ')'
    if multiLineFunction:
      res += '\nend\n'
    else:
      res += ' end\n'
    return res

  ## enums
  def _generateCVMEnum(self, c):
    name = c.spelling
    constants = []
    for ch in c.get_children():
      if ch.kind == CK.ENUM_CONSTANT_DECL:
        value = ''
        for ca in ch.get_children():
          if ca.kind == CK.UNEXPOSED_EXPR:
            value = ' = ' + getContent(ca, False)
            break
        constants.append('  ' + ch.spelling + value)

    # handle forward decleration of enums, such as: ImGuiKey & ImGuiMouseSource.
    if len(constants) == 0:
      res = 'typedef ' + c.enum_type.get_canonical().spelling + ' ' + name + ';\n'
      return res

    #res = res + '\ntypedef int ' + name + ';\n'
    res = self.getCursorDebug(c, '// ') + '\n'
    res = res + 'typedef enum {\n' + ',\n'.join(constants) + '\n} ' + name + ';\n'
    return res

  def _generateLVMEnum(self, c):
    res = '--=== enum ' + c.spelling + ' ===\n'
    for ch in c.get_children():
      if ch.kind == CK.ENUM_CONSTANT_DECL:
        lname = ch.spelling
        if lname[:5] == 'ImGui':
          lname = lname[5:]
        res += 'M.' + lname + ' = C.' + ch.spelling + '\n'
    res += '--===\n'
    return res

  ## main
  def _traverse(self, c, level):
    if c.location.file and not c.location.file.name.endswith(self.sFilename):
      return

    if c.get_usr() in skip_usrs or c.spelling in skip_names or c.kind == CK.CLASS_TEMPLATE or c.kind == CK.FUNCTION_TEMPLATE:
      #print(' --  skipping: ', str(c.kind)[str(c.kind).index('.')+1:], c.type.spelling, c.spelling)
      return

    if c.kind == CK.FUNCTION_DECL or c.kind == CK.CXX_METHOD:
      # do not add operators
      if c.spelling.find('operator') == 0:
        return
      self.tVMFile.write(self._generateCVMFunction(c, 'imgui_', None))
      self.tHostFile.write(self._generateCHostFunction(c, 'imgui_', 'ImGui::', None, None))
      self.tLuaFile.write(self._generateLuaVMFunction(c, '', 'imgui_', None))
      return

    elif c.kind == CK.TYPEDEF_DECL:
      txt = getContent(c, False)
      self.tVMFile.write(txt + ";\n")
      return

    elif c.kind == CK.STRUCT_DECL or c.kind == CK.UNION_DECL:
      if(c.is_definition()):
        self.tVMFile.write(self._generateCVMStruct(c, 0))
        self.tHostFile.write(self._generateCHostStruct(c))
        self.tLuaFile.write(self._generateLVMStruct(c))
      else:
        # fwd decl
        self.tVMFile.write('typedef struct ' + c.spelling + ' ' + c.spelling + ';\n')
        #print(c, c.spelling)
        #self.tVMFile.write(getContent(c, False) + ";\n")
      return

    elif c.kind == CK.ENUM_DECL:
      self.tVMFile.write(self._generateCVMEnum(c))
      self.tLuaFile.write(self._generateLVMEnum(c))
      return

    elif c.kind == CK.TRANSLATION_UNIT:
      pass
    elif c.kind == CK.NAMESPACE:
      #ctx.tVMFile.write('\n// namespace ' + c.spelling + '\n')
      pass

    else:
      print('* unhandled item: ' + ' ' * level, str(c.kind)[str(c.kind).index('.')+1:], c.type.spelling, c.spelling)
      print(' ' * level, '  ', getContent(c, True))

    for cn in c.get_children():
      self._traverse(cn, level + 1)

  def generate(self, c, sFilename):
    self.sFilename = sFilename
    outDir = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'generated')
    if not os.path.exists(outDir): os.mkdir(outDir)
    #timestampStr = str(datetime.datetime.now())

    with open(os.path.join(outDir, 'imgui_gen.lua'), 'w') as self.tLuaFile:
      self.tLuaFile.write("""-- !!!! DO NOT EDIT THIS FILE -- It was automatically generated by tools/imguiUtilities/gen.py at git repo beamng/gameengine -- DO NOT EDIT THIS FILE !!!!

local C = ffi.C -- shortcut to prevent lookups all the time

return function(M)

""")
      #self.tLuaFile.write('-- generated on ' + timestampStr + '\n\n')

      with open(os.path.join(outDir, 'imgui_gen.h'), 'w') as self.tVMFile:
        #self.tVMFile.write('// generated on ' + timestampStr + '\n\n')
        self.tVMFile.write("""///////////////////////////////////////////////////////////////////////////////
// this file is used for declaring C types for LuaJIT's FFI. Do not use it in C
///////////////////////////////////////////////////////////////////////////////

// !!!! DO NOT EDIT THIS FILE -- It was automatically generated by tools/imguiUtilities/gen.py at git repo beamng/gameengine -- DO NOT EDIT THIS FILE !!!!

typedef struct { float x, y; } ImVec2_C;
typedef struct { float x, y, z, w; } ImVec4_C;

""")

        with open(os.path.join(outDir, 'imguiApiHostGenerated.cpp'), 'w') as self.tHostFile:
          #self.tHostFile.write('// generated on ' + timestampStr + '\n\n')
          self.tHostFile.write("""// !!!! DO NOT EDIT THIS FILE -- It was automatically generated by tools/imguiUtilities/gen.py at git repo beamng/gameengine -- DO NOT EDIT THIS FILE !!!!

#include "imguiApiHost.h"
extern "C" {
#ifdef BNG_OS_WINDOWS
#define FFI_EXPORT __declspec(dllexport)
#else
#define FFI_EXPORT __attribute__((visibility("default")))
#endif // BNG_OS_WINDOWS

""")
          self.detectOverloads(c)
          self._traverse(c, 0)
          self.tHostFile.write("""

#undef FFI_EXPORT
} // extern C
""")
      self.tLuaFile.write("""
end -- global function close
""")

  def getCursorDebug(self, c, prefix):
    if not self.debug:
      return ''
    else:
      return prefix + c.get_usr()

  # ability to change the function name when overloads exist
  def getFunctionName(self, c):
    u = c.get_usr()
    if u in self.functionRenames:
      return self.functionRenames[u]
    else:
      return c.spelling

  def detectOverloads(self, c):
    fctCache = {}
    self._rec_detectOverloads(fctCache, c, 0, '')

    # delete all elements that have only one value - no overloads
    for k in list(fctCache.keys()):
      if len(fctCache[k]) == 1:
        del(fctCache[k])

    for k, v in fctCache.items():
      for i in range(len(v)):
        self.functionRenames[v[i].get_usr()] = v[i].spelling + str(i + 1)

    #pp = pprint.PrettyPrinter(depth=6)
    #pp.pprint(fctCache)
    #pp.pprint(self.functionRenames)

  def _rec_detectOverloads(self, fctCache, c, level, prefix):
    #print(c.kind, c.get_usr(), c.spelling)
    if c.kind == CK.STRUCT_DECL or c.kind == CK.TRANSLATION_UNIT or c.kind == CK.NAMESPACE:
      prefix += c.spelling + '_'

    elif c.kind == CK.FUNCTION_DECL or c.kind == CK.CXX_METHOD:
      # do not add operators
      if c.spelling.find('operator') == 0:
        return

      uName = prefix + c.spelling
      if not uName in fctCache: fctCache[uName] = []

      usr = c.get_usr()
      # We need to filter out the double definitions
      contained = False
      for f in fctCache[uName]:
        if f.get_usr() == usr:
          contained = True
          break
      if not contained:
        fctCache[uName].append(c)

    for cn in c.get_children():
      self._rec_detectOverloads(fctCache, cn, level, prefix)

  def getCFunctionSignature(self, c, prefix, firstArg, isHost):
    parameters = []
    parameter_names = []
    parameter_deref = []
    isVariadic = False
    i = 0
    for p in c.get_arguments():
      i += 1
      parameters.append(getCVarStr(p, False))

      dereferenceRequired = False
      if p.type.kind == TyK.LVALUEREFERENCE:
        dereferenceRequired = True
      parameter_names.append(luaParameterSpelling(p, False))
      parameter_deref.append(dereferenceRequired)

    if c.type.is_function_variadic():
      isVariadic = True
      parameters.append('...')
    if firstArg:
      parameters.insert(0, firstArg)

    resStr = 'return '
    resType = c.result_type.spelling

    effectiveReturnType = c.result_type
    if c.result_type.kind == TyK.TYPEDEF:
      effectiveReturnType = c.result_type.get_canonical()

    if isHost:
      if effectiveReturnType.spelling == 'ImVec2':
        resType = 'ImVec2_C'
      elif effectiveReturnType.spelling == 'ImVec4' or effectiveReturnType.spelling == 'ImColor':
        resType = 'ImVec4_C'

    if resType == 'void':
      resStr = ''
    return (resType + ' ' + prefix + self.getFunctionName(c) + '(' + ', '.join(parameters) + ')'), resStr, parameter_names, isVariadic, parameter_deref

def main():
  if len(sys.argv) != 2:
    print("Usage: gen.py [input]")
    print("Example: gen.py imgui.h")
    sys.exit(1)

  sFilename = sys.argv[1]

  # use clang to parse
  if os.name == 'nt':
    clang.cindex.Config.set_library_file('C:/Program Files/LLVM/bin/libclang.dll')
  else:
    clang.cindex.Config.set_library_file('/usr/lib/llvm-6.0/lib/libclang.so')
  index = clang.cindex.Index.create()

  translation_unit = index.parse(sFilename, ['-x', 'c++', '-std=c++11', '-D__CODE_GENERATOR__', '-DIMGUI_DISABLE_OBSOLETE_FUNCTIONS'])

  # now walk the AST and write the result file
  BindingGenerator(debug).generate(translation_unit.cursor, sFilename)

  #dumpCursor(translation_unit.cursor, 0)
  print("SUCCESS!")
  print("Output files are available at 'generated/*'")

if __name__ == '__main__':
  main()
