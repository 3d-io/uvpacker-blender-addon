# Copyright (c) 2021 Boris Posavec
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

bl_info = {
  "name": "UV-Packer",
  "description": "Automated, fast, accurate, free UV-Packing",
  "blender": (2, 90, 0),
  "version" : (1, 0, 0),
  "category": "UV",
  "author": "Boris Posavec",
  "location": "UV Editing > Sidebar > UV-Packer",
  "wiki_url": "https://doc.uv-packer.com",
  "tracker_url": "https://discord.gg/r8jPCWk",
  "support": "COMMUNITY",
}

import bpy
import bmesh
import os
import webbrowser
import subprocess
import time
import queue
import threading
import struct
from bpy.props import (StringProperty, BoolProperty, IntProperty, FloatProperty, FloatVectorProperty, EnumProperty, PointerProperty)
from bpy.types import (Panel, Menu, Operator, PropertyGroup, AddonPreferences)

class misc:
  UV_PACKER_MAP_NAME = "UV-Packer"

  def set_map_name(name):
    global UV_PACKER_MAP_NAME
    UV_PACKER_MAP_NAME = name
    return

  def get_map_name():
    global UV_PACKER_MAP_NAME
    return UV_PACKER_MAP_NAME

  def add_uv_channel_to_objects(objects):
    for obj in objects:
      if obj.type != "MESH":
        continue
      found = False
      for uv_layer in obj.data.uv_layers:
        if uv_layer.name == misc.get_map_name():
          found = True
          continue
      if found == False:
        obj.data.uv_layers.new(name=misc.get_map_name())
      obj.data.uv_layers.active = obj.data.uv_layers[misc.get_map_name()]
    return

  def remove_uv_channel_from_objects(objects, name):
    for obj in objects:
      if obj.type != "MESH":
        continue
      uvs = obj.data.uv_layers
      if name in uvs:
        uvs.remove(uvs[name])
    return

  def gather_object_data(obj):
    bm = bmesh.from_edit_mesh(obj.data)
    bm.normal_update()
    bm.verts.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    uv_layer = bm.loops.layers.uv.verify()

    data = bytearray()
    nameBytes = obj.name.encode()
    data += (len(nameBytes)).to_bytes(4, byteorder="little")
    data.extend(nameBytes)

    data += (len(bm.verts)).to_bytes(4, byteorder="little")
    for vert in bm.verts:
      data += bytearray(struct.pack("<ddd", vert.co.x, vert.co.y, vert.co.z))

    indexCount = 0
    data += (len(bm.faces)).to_bytes(4, byteorder="little")
    for i, face in enumerate(bm.faces):
      data += (len(face.loops)).to_bytes(4, byteorder="little")
      for loop in face.loops:
        vert = loop.vert
        data += (vert.index).to_bytes(4, byteorder="little")
        data += bytearray(struct.pack("<ddd", vert.normal.x, vert.normal.y, vert.normal.z))
        uv_coord = loop[uv_layer].uv
        data += bytearray(struct.pack("<dd", uv_coord.x, uv_coord.y))
        data += (indexCount).to_bytes(4, byteorder="little")
        indexCount += 1

    return data

  def replace_object_data(obj, message, readPtr):
    bm = bmesh.from_edit_mesh(obj.data)
    bm.verts.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    uv_layer = bm.loops.layers.uv.verify()
    faces = [f for f in bm.faces]

    numResultVerts = struct.unpack_from("<I", message, readPtr)[0]
    readPtr += 4

    for face in faces:
      for loop in face.loops:
        x = struct.unpack_from("<d", message, readPtr)[0]
        readPtr += 8
        y = struct.unpack_from("<d", message, readPtr)[0]
        readPtr += 8
        loop[uv_layer].uv = [x, y]

    bmesh.update_edit_mesh(obj.data, False, False)
    return readPtr

  class QueueMessage:
    MESSAGE = 0
    PROGRESS = 1
    STATS = 2
    COMPLETE = 3

  class QueueMsgSeverity:
    INFO = 0
    WARNING = 1
    ERROR = 2

  def encodeOptions(options):
    data = bytearray()
    data += (options["PackMode"]).to_bytes(4, byteorder="little")
    data += (options["Width"]).to_bytes(4, byteorder="little")
    data += (options["Height"]).to_bytes(4, byteorder="little")
    data += bytearray(struct.pack("<d", options["Padding"]))
    data += bytearray(struct.pack("<?", options["Combine"]))
    data += bytearray(struct.pack("<?", options["Rescale"]))
    data += bytearray(struct.pack("<?", options["PreRotate"]))
    data += bytearray(struct.pack("<?", options["FullRotation"]))
    data += (options["Rotation"]).to_bytes(4, byteorder="little")
    data += (options["TilesX"]).to_bytes(4, byteorder="little")
    data += (options["TilesY"]).to_bytes(4, byteorder="little")
    return data

  def data_exchange_thread(process, options, meshes, msg_queue):
    numObjects = len(meshes)
    if numObjects == 0:
      msg_queue.put((misc.QueueMessage.MESSAGE, "No objects to pack.", misc.QueueMsgSeverity.ERROR))
      return

    msg_queue.put((misc.QueueMessage.MESSAGE, "Preparing geometry"))

    binaryData = bytearray()
    binaryData += (bl_info["version"][0]).to_bytes(4, byteorder="little")
    binaryData += (bl_info["version"][1]).to_bytes(4, byteorder="little")
    binaryData += (bl_info["version"][2]).to_bytes(4, byteorder="little")
    binaryData += misc.encodeOptions(options)
    binaryData += (numObjects).to_bytes(4, byteorder="little")

    for object_idx, obj in enumerate(meshes):
      binaryData += (object_idx).to_bytes(4, byteorder="little")
      binaryData += misc.gather_object_data(obj)

    sumBytes = len(binaryData)
    binaryData = sumBytes.to_bytes(4, byteorder="little") + binaryData

    msg_queue.put((misc.QueueMessage.MESSAGE, "Packing"))

    try:
      out_stream = process.stdin
      out_stream.write(binaryData)
      out_stream.flush()

      message = ""
      while True:
        messageSize = struct.unpack("<I", process.stdout.read(4))[0]
        message = process.stdout.read(messageSize)
        readPtr = 0
        messageType = struct.unpack_from("<I", message, readPtr)[0]
        readPtr += 4
        if messageType == 0: # success
          break
        elif messageType == 1: # progress
          msg_queue.put((misc.QueueMessage.PROGRESS, struct.unpack_from("<d", message, readPtr)[0]))
        elif messageType == 2: # error
          msgSize = struct.unpack_from("<I", message, readPtr)[0]
          readPtr += 4
          msg = message[readPtr:readPtr+msgSize].decode()
          msg_queue.put((misc.QueueMessage.MESSAGE, msg, misc.QueueMsgSeverity.ERROR))
          return
        else:
          print("Error: unsupported message " + str(messageType))

      numObjects = struct.unpack_from("<I", message, readPtr)[0]
      readPtr += 4
      for obj in range(0, numObjects):
        objId = struct.unpack_from("<I", message, readPtr)[0]
        readPtr += 4
        nameSize = struct.unpack_from("<I", message, readPtr)[0]
        readPtr += 4
        objName = message[readPtr:readPtr+nameSize].decode()
        readPtr += nameSize
        readPtr = misc.replace_object_data(meshes[objId], message, readPtr)

      coverage = struct.unpack_from("<d", message, readPtr)[0]
      msg_queue.put((misc.QueueMessage.STATS, str(round(coverage, 2))))
      msg_queue.put((misc.QueueMessage.MESSAGE, "Packing complete", misc.QueueMsgSeverity.WARNING))
    except:
      return

  def get_meshes(objects):
    return [obj for obj in objects if obj.type=="MESH"]

  def get_unique_objects(objects):
    unique_meshes = []
    unique_objects = []
    for obj in objects:
      if obj.data in unique_meshes:
        continue
      unique_meshes.append(obj.data)
      unique_objects.append(obj)
    return unique_objects

  def resolve_engine(engine_str):
    if engine_str == "OP0":
      return 0
    elif engine_str == "OP1":
      return 1
    else:
      return 0

  def ShowMessageBox(message = "", title = "Message Box", icon = "INFO"):
    def draw(self, context):
      self.layout.label(text=message)
    bpy.context.window_manager.popup_menu(draw, title = title, icon = icon)
    return

class UVPackProperty(PropertyGroup):
  uvp_combine: BoolProperty(name="Combine", description="Pack all selected objects in one UV Sheet", default = True)
  uvp_width: IntProperty(name="w:", description="UV Sheet Width", default = 1024, min = 8)
  uvp_height: IntProperty(name="h:", description="UV Sheet Height", default = 1024, min = 8)
  uvp_padding: FloatProperty(name="Padding", description="UV Sheet Padding", default = 2.0, min = 0.0)
  uvp_engine: EnumProperty(
    name="Dropdown:",
    description="Chose Packing method",
    items=
    [
    ("OP0", "Efficient", "Best compromise for speed and space usage"),
    ("OP1", "High Quality", "Slowest but maximal space usage"),
    ],
    default="OP0"
    )
  uvp_rescale: BoolProperty(name="Rescale UV-Charts", description="Rescale UV-Charts", default = True)
  uvp_prerotate: BoolProperty(name="Pre-Rotate", description="Pre-rotate UV-Charts", default = True)
  uvp_rotate: EnumProperty(
    name="Rotation:",
    description="Choose Rotation",
    items=
    [
      ("0", "0", "None"),
      ("1", "90", "90 degrees"),
      ("2", "45", "45 degrees"),
      ("3", "23", "23 degrees")
    ],
    default="1"
    )
  uvp_fullRotate: BoolProperty(name="Ø", description="Use full rotation", default = False)
  uvp_tilesX: IntProperty(name="Tiles X:", description="UV Tile Columns", default = 1, min = 1)
  uvp_tilesY: IntProperty(name="Tiles Y:", description="UV Tile Rows", default = 1, min = 1)
  uvp_create_channel: BoolProperty(name="Create new map channel", description="Create new Map channel for UV-Packer to store the results into", default = False)
  uvp_channel_name: StringProperty(name="UV Map", description="Set name for the created channel", default="UV-Packer")
  uvp_stats: StringProperty(name="Stats", description="Stats", default="0.0%  ¦  0s")

class UVPackerPanel(bpy.types.Panel):
  bl_label = "UV-Packer"
  bl_idname = "UVP_PT_layout"
  bl_category = "UV-Packer"
  bl_space_type = "IMAGE_EDITOR"
  bl_region_type = "UI"

  @classmethod
  def poll(self, context):
    return context.object is not None

  def draw(self, context):
    layout = self.layout
    scene = context.scene
    packerProps = scene.UVPackerProps
    obj = context.object

    mesh = bpy.context.object.data
    uv_map = mesh.uv_layers.active

    row = layout.row()
    row.scale_y = 3.0
    row.operator("uvpackeroperator.packbtn", text="Pack")
    row = layout.row(align=True)
    row.prop(packerProps, "uvp_combine")
    
    row = layout.row()
    row.label(text="≡ UV Sheet:")
    row.label(text=packerProps.uvp_stats)
    row = layout.row(align=True)
    row.scale_y = 1.5
    row.operator("uvpackeroperator.sizebtn", text="512").size = 512
    row.operator("uvpackeroperator.sizebtn", text="1k").size = 1024
    row.operator("uvpackeroperator.sizebtn", text="2k").size = 2048
    row.operator("uvpackeroperator.sizebtn", text="4k").size = 4096

    row = layout.row(align=True)
    row.alignment = "EXPAND"
    row.prop(packerProps, "uvp_width")
    row.prop(packerProps, "uvp_height")
    layout.prop(packerProps, "uvp_padding")
    layout.separator()

    layout.label(text="≡ UV Packing Engine:")
    layout.prop(packerProps, "uvp_engine", text="Type")
    layout.prop(packerProps, "uvp_rescale")
    layout.prop(packerProps, "uvp_prerotate")

    row = layout.row(align=True)
    row.scale_y = 1.5
    row.prop(packerProps, "uvp_rotate", expand=True)
    row.prop(packerProps, "uvp_fullRotate", toggle=True)

    row = layout.row(align=True)
    row.prop(packerProps, "uvp_tilesX")
    row.prop(packerProps, "uvp_tilesY")

    layout.separator()
    layout.label(text="≡ UV Channel Controls:")
    layout.prop(packerProps, "uvp_create_channel")
    layout.prop(packerProps, "uvp_channel_name")
    layout.operator("uvpackeroperator.clearmaptoolbtn", text="Remove Map From Selection")

    layout.separator()
    versionStr = "UV-Packer Version: %d.%d.%d" % bl_info["version"]
    layout.label(text=versionStr, icon="SETTINGS") 
    row = layout.row()
    row.scale_y = 1.5
    row.operator("wm.url_open", text="UV-Packer Homepage", icon="HOME").url = "https://www.uv-packer.com"
    row = layout.row()
    row.scale_y = 1.5
    row.operator("wm.url_open", text="Documentation" , icon="QUESTION").url = "https://doc.uv-packer.com/"

class UVPackerPackButtonOperator(Operator):
  bl_idname = "uvpackeroperator.packbtn"
  bl_label = "Pack"
  bl_options = {"REGISTER", "UNDO"}
  bl_description = "Pack selected UVs"

  def update_status(self, msg, severity="INFO"):
    self.report({severity}, msg)

  def execute(self, context):
    self.timer = time.time()
    self.coverage = 0.0
    packer_props = context.scene.UVPackerProps
    packer_props.dbg_msg = ""
    
    if len(bpy.context.selected_objects) == 0:
      return {"FINISHED"}

    options = {
      "PackMode": misc.resolve_engine(packer_props.uvp_engine),
      "Width": packer_props.uvp_width,
      "Height": packer_props.uvp_height,
      "Padding": packer_props.uvp_padding,
      "Rescale": packer_props.uvp_rescale,
      "PreRotate": packer_props.uvp_prerotate,
      "Rotation": int(packer_props.uvp_rotate),
      "FullRotation": packer_props.uvp_fullRotate,
      "Combine": packer_props.uvp_combine,
      "TilesX": packer_props.uvp_tilesX,
      "TilesY": packer_props.uvp_tilesY
    }

    packerDir = os.path.dirname(os.path.realpath(__file__))
    packerExe = packerDir + "\\UV-Packer-Blender.exe"

    try:
      self.process = subprocess.Popen([packerExe], stdin=subprocess.PIPE, stdout=subprocess.PIPE, shell=False)
    except:
      msgStr = "UV-Packer executable not found. Please copy UV-Packer-Blender.exe to: " + packerDir
      self.update_status(msgStr, "ERROR")
      return {"FINISHED"}

    wm = context.window_manager
    wm.modal_handler_add(self)

    unique_objects = misc.get_unique_objects(context.selected_objects)
    meshes = misc.get_meshes(unique_objects)

    if packer_props.uvp_create_channel:
      misc.set_map_name(packer_props.uvp_channel_name)
      misc.add_uv_channel_to_objects(unique_objects)

    bpy.ops.object.mode_set(mode = "EDIT")
    self.msg_queue = queue.SimpleQueue()

    self.packer_thread = threading.Thread(target=misc.data_exchange_thread, args=(self.process, options, meshes, self.msg_queue))
    self.packer_thread.daemon = True
    self.packer_thread.start()
    return {"RUNNING_MODAL"}

  def modal(self, context, event):
    self.CheckUserCancel(event)
    if self.CheckMessages():
      context.scene.UVPackerProps.uvp_stats = f"{self.coverage}% ¦ {round(time.time() - self.timer, 2)}s"
      bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)
      return {"FINISHED"}
    if not self.packer_thread.is_alive() and self.process.poll() is not None:
      self.msg_queue.put((misc.QueueMessage.COMPLETE, 1))
    return {"RUNNING_MODAL"}

  def CheckUserCancel(self, event):
    if event.type == "ESC":
      self.process.terminate()
      self.update_status("UV-Packer cancelled")

  def CheckMessages(self):
    while True:
      try:
        item = self.msg_queue.get_nowait()
      except queue.Empty as ex:
        break

      if item[0] == misc.QueueMessage.PROGRESS:
        progress_str = "Progress: %d %%" % (int(item[1] * 100.0))
        self.update_status(progress_str)
      elif item[0] == misc.QueueMessage.MESSAGE:
        if (len(item) > 2):
          if (item[2] == misc.QueueMsgSeverity.WARNING):
            self.update_status(item[1], "WARNING")
          elif (item[2] == misc.QueueMsgSeverity.ERROR):
            self.update_status(item[1], "ERROR")
            misc.ShowMessageBox(item[1], "Error", "ERROR")
          else:
            self.update_status(item[1], "INFO")
        else:
          self.update_status(item[1], "INFO")
      elif item[0] == misc.QueueMessage.STATS:
        self.coverage = item[1]
      elif item[0] == misc.QueueMessage.COMPLETE:
        return True
    return False

class UVPackerSizeButtonOperator(Operator):
  bl_idname = "uvpackeroperator.sizebtn"
  bl_label = "Size"
  bl_description = "UV Sheet dimension"
  size: bpy.props.IntProperty()

  def execute(self, context):
    context.scene.UVPackerProps.uvp_width = self.size
    context.scene.UVPackerProps.uvp_height = self.size
    return {"FINISHED"}

class UVPackerRotationButtonOperator(Operator):
  bl_idname = "uvpackeroperator.rotbtn"
  bl_label = "Rotation"
  rotation: bpy.props.IntProperty()

  def execute(self, context):
    context.scene.UVPackerProps.uvp_rotate = self.rotation
    return {"FINISHED"}

class UVPackerFullRotationButtonOperator(Operator):
  bl_idname = "uvpackeroperator.fullrotbtn"
  bl_label = "Full Rotation"

  def execute(self, context):
    context.scene.UVPackerProps.uvp_fullRotate = not context.scene.UVPackerProps.uvp_fullRotate
    return {"FINISHED"}

class UVPackerToolClearMapButtonOperator(Operator):
  bl_idname = "uvpackeroperator.clearmaptoolbtn"
  bl_label = "Remove UV map from selected"
  bl_description = "Delete this UV Map from selected object(s)"

  def execute(self, context):
    name = context.scene.UVPackerProps.uvp_channel_name
    misc.remove_uv_channel_from_objects(bpy.context.selected_objects, name)
    return {"FINISHED"}

registered_classes = []
classes = (UVPackProperty, UVPackerPanel, UVPackerPackButtonOperator, UVPackerSizeButtonOperator,
UVPackerRotationButtonOperator, UVPackerFullRotationButtonOperator, UVPackerToolClearMapButtonOperator)

def register():
  if bpy.app.version < (2, 83, 0):
    return

  from bpy.utils import register_class
  for cls in classes:
    bpy.utils.register_class(cls)
    registered_classes.append(cls)

  bpy.types.Scene.UVPackerProps = PointerProperty(type=UVPackProperty)

def unregister():
  from bpy.utils import unregister_class
  for cls in registered_classes:
    bpy.utils.unregister_class(cls)
  del bpy.types.Scene.UVPackerProps

if __name__ == "__main__":
  register()
