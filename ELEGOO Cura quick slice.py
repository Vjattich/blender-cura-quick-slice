bl_info = {
    "name": "ELEGOO Cura quick slice",
    "author": "Vjattich",
    "version": (1, 0, 0),
    "blender": (3, 0, 0),
    "location": "F6 / Shift+F6 / File > Export > Quick Slice",
    "description": "Slice the active collection with CuraEngine into the .blend folder",
    "category": "Import-Export",
}

import bpy, os, re, json, math, zlib, shutil, tempfile, subprocess, time, glob
from bpy.props import (StringProperty, FloatProperty, BoolProperty, EnumProperty,
                       IntProperty, CollectionProperty)


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------

class QuickSliceProfile(bpy.types.PropertyGroup):
    name: StringProperty(
        name="Name", default="profile",
        description="Shown in the F6 menu")

    path: StringProperty(
        name="Profile .json", subtype="FILE_PATH",
        description="Printer definition exported from Cura (inherits fdmprinter)")

    suffix: StringProperty(
        name="Suffix",
        description="Appended to the output name, e.g. '_tpu' -> part_tpu.gcode. "
                    "Leave empty to keep the plain name")

    overrides: StringProperty(
        name="Overrides",
        description="Comma separated key=value applied on top of the global ones")


class QuickSlicePreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    cura_root: StringProperty(
        name="Cura folder", subtype="DIR_PATH",
        default=r"C:\Program Files\ELEGOO_Cura",
        description="Install folder holding CuraEngine.exe and resources/definitions")

    profiles: CollectionProperty(type=QuickSliceProfile)

    # Remembered silently so Shift+F6 repeats whatever was sliced last.
    last_used: IntProperty(default=0, min=0)

    scale: FloatProperty(
        name="Scale", default=1.0, min=0.0001, soft_max=1000.0,
        description="Blender unit -> mm. 1.0 if you model in mm, 1000.0 if in meters")

    name_from: EnumProperty(
        name="Name from", default="BLEND",
        items=[("COLLECTION", "Collection", "Active collection name"),
               ("BLEND", "Blend file", ".blend filename"),
               ("FOLDER", "Folder", "Containing folder name")])

    overrides: StringProperty(
        name="Overrides",
        description="Comma separated key=value applied to every profile, "
                    "e.g. layer_height=0.3, infill_sparse_density=10")

    verbose: BoolProperty(name="Verbose engine log", default=False)
    open_folder: BoolProperty(name="Open folder when done", default=False)
    keep_stl: BoolProperty(name="Keep exported STL", default=True)

    def draw(self, context):
        column = self.layout.column()
        column.prop(self, "cura_root")

        row = column.row(align=True)
        row.prop(self, "scale")
        row.prop(self, "name_from")

        column.prop(self, "overrides", text="Global overrides")

        row = column.row(align=True)
        row.prop(self, "verbose")
        row.prop(self, "open_folder")
        row.prop(self, "keep_stl")

        column.separator()
        header = column.row(align=True)
        header.label(text="Profiles", icon="PRESET")
        header.operator(QUICKSLICE_OT_profile_add.bl_idname, text="Add", icon="ADD")

        if not len(self.profiles):
            column.label(text="no profiles yet - press Add", icon="INFO")
            return

        for index, profile in enumerate(self.profiles):
            box = column.box()

            row = box.row(align=True)
            row.prop(profile, "name", text="")
            move_up = row.operator(QUICKSLICE_OT_profile_move.bl_idname,
                                   text="", icon="TRIA_UP")
            move_up.index, move_up.step = index, -1
            move_down = row.operator(QUICKSLICE_OT_profile_move.bl_idname,
                                     text="", icon="TRIA_DOWN")
            move_down.index, move_down.step = index, 1
            remove = row.operator(QUICKSLICE_OT_profile_remove.bl_idname,
                                  text="", icon="X")
            remove.index = index

            box.prop(profile, "path", text="")
            box.prop(profile, "suffix")
            box.prop(profile, "overrides")


def get_preferences():
    """This add-on's preferences block."""
    return bpy.context.preferences.addons[__name__].preferences


def get_profile(index):
    """Return (index, profile). A negative index means "the one used last"."""
    preferences = get_preferences()
    if index < 0:
        index = preferences.last_used
    if not (0 <= index < len(preferences.profiles)):
        raise RuntimeError("no slicing profile - add one in the add-on preferences")
    return index, preferences.profiles[index]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def absolute_path(value):
    """Expand Blender's // paths. An empty setting stays empty."""
    value = (value or "").strip()
    return os.path.normpath(bpy.path.abspath(value)) if value else ""


def sanitize_filename(text):
    """Strip characters Windows refuses in a file name."""
    return re.sub(r'[<>:"/\\|?*]', "_", text or "").strip()


def parse_overrides(*texts):
    """Turn "key=value" lists into a dict.

    Values are read left to right, so a profile override wins over a global one.
    """
    settings = {}
    for text in texts:
        for chunk in re.split(r"[,;\n]", text or ""):
            chunk = chunk.strip()
            if not chunk:
                continue
            if "=" not in chunk:
                raise RuntimeError("bad override %r - expected key=value" % chunk)
            key, value = chunk.split("=", 1)
            settings[key.strip()] = value.strip()
    return settings


# ---------------------------------------------------------------------------
# Finding Cura on disk
# ---------------------------------------------------------------------------

def find_cura_engine(cura_root):
    """Path to the slicer executable inside the Cura install folder."""
    for name in ("CuraEngine.exe", "CuraCLI.exe"):
        candidate = os.path.join(cura_root, name)
        if os.path.isfile(candidate):
            return candidate

    found = glob.glob(os.path.join(cura_root, "**", "CuraEngine.exe"), recursive=True)
    if found:
        return found[0]
    raise RuntimeError("CuraEngine.exe not found under " + cura_root)


def find_definitions_dir(cura_root):
    """Folder holding fdmprinter.def.json, which every profile inherits from."""
    for relative in (r"share\cura\resources\definitions", r"resources\definitions"):
        candidate = os.path.join(cura_root, relative)
        if os.path.isfile(os.path.join(candidate, "fdmprinter.def.json")):
            return candidate

    found = glob.glob(os.path.join(cura_root, "**", "fdmprinter.def.json"), recursive=True)
    if found:
        return os.path.dirname(found[0])
    raise RuntimeError("fdmprinter.def.json not found under " + cura_root)


# ---------------------------------------------------------------------------
# Building a printer definition CuraEngine can load
# ---------------------------------------------------------------------------

# fdmextruder declares these itself, so its defaults shadow the printer .json
# unless we copy them into the generated extruder train.
EXTRUDER_KEYS = (
    "extruder_nr", "machine_nozzle_id", "machine_nozzle_size",
    "machine_nozzle_offset_x", "machine_nozzle_offset_y",
    "machine_extruder_start_code", "machine_extruder_start_pos_abs",
    "machine_extruder_start_pos_x", "machine_extruder_start_pos_y",
    "machine_extruder_end_code", "machine_extruder_end_pos_abs",
    "machine_extruder_end_pos_x", "machine_extruder_end_pos_y",
    "extruder_prime_pos_x", "extruder_prime_pos_y", "extruder_prime_pos_z",
    "machine_extruder_cooling_fan_number", "material_diameter",
)


def workdir_name(profile_name):
    """Readable folder name plus a short hash, so two profiles never collide."""
    readable = re.sub(r"[^A-Za-z0-9_.-]", "_", profile_name or "").strip("_") or "profile"
    checksum = "%08x" % (zlib.crc32((profile_name or "").encode("utf-8")) & 0xffffffff)
    return readable[:32] + "_" + checksum[:6]


def build_printer_definition(definition_json, definitions_dir, profile_name):
    """Copy the profile into a private temp folder and write its extruder trains.

    Cura's GUI generates these train files on the fly; the CLI expects them on
    disk. Each profile gets its own folder so two profiles never overwrite one
    another's trains.

    Returns (workdir, printer_definition_path, definition_document).
    """
    workdir = os.path.join(tempfile.gettempdir(), "blender_quick_slice",
                           workdir_name(profile_name))
    os.makedirs(workdir, exist_ok=True)

    with open(definition_json, encoding="utf-8") as handle:
        definition = json.load(handle)

    printer_id = definition.get("name") or "quick_slice"
    printer_definition = os.path.join(workdir, printer_id + ".def.json")
    shutil.copyfile(definition_json, printer_definition)

    printer_overrides = definition.get("overrides", {})
    trains = definition.get("metadata", {}).get(
        "machine_extruder_trains", {"0": printer_id + "_extruder_0"})

    for number, train_id in trains.items():
        if os.path.isfile(os.path.join(definitions_dir, train_id + ".def.json")):
            continue                          # Cura already ships this train

        train_overrides = {"extruder_nr": {"default_value": int(number)}}
        for key in EXTRUDER_KEYS:
            if key != "extruder_nr" and key in printer_overrides:
                train_overrides[key] = printer_overrides[key]

        train = {
            "version": 2,
            "name": "Extruder %s" % number,
            "inherits": "fdmextruder",
            "metadata": {"machine": printer_id, "position": str(number)},
            "overrides": train_overrides,
        }
        train_path = os.path.join(workdir, train_id + ".def.json")
        with open(train_path, "w", encoding="utf-8") as handle:
            json.dump(train, handle, indent=2)

    return workdir, printer_definition, definition


# ---------------------------------------------------------------------------
# Blender side
# ---------------------------------------------------------------------------

def active_collection_meshes():
    """The collection selected in the outliner, plus its meshes (children too)."""
    layer_collection = bpy.context.view_layer.active_layer_collection
    collection = (layer_collection.collection if layer_collection
                  else bpy.context.scene.collection)
    meshes = [obj for obj in collection.all_objects if obj.type == "MESH"]
    return collection, meshes


def output_stem(preferences, collection):
    """File name for the STL and the gcode, without extension."""
    blend_name = os.path.splitext(os.path.basename(bpy.data.filepath))[0]
    scene_root = bpy.context.scene.collection

    if preferences.name_from == "FOLDER":
        raw = os.path.basename(os.path.dirname(bpy.data.filepath))
    elif preferences.name_from == "COLLECTION" and collection is not scene_root:
        raw = collection.name
    else:
        # "Blend file", or the scene root, whose name is never interesting.
        raw = blend_name

    return sanitize_filename(raw) or blend_name or "scene"


def bounding_box_mm(objects, scale):
    """Overall size of the objects in millimetres, or None if there are none."""
    from mathutils import Vector

    xs, ys, zs = [], [], []
    for obj in objects:
        for corner in obj.bound_box:
            world = obj.matrix_world @ Vector(corner)
            xs.append(world.x)
            ys.append(world.y)
            zs.append(world.z)

    if not xs:
        return None
    return ((max(xs) - min(xs)) * scale,
            (max(ys) - min(ys)) * scale,
            (max(zs) - min(zs)) * scale)


def export_stl(path, objects, scale):
    """Export the given objects, restoring the user's selection afterwards.

    Both STL exporters can only filter by selection, so the collection has to be
    selected for the duration of the export.
    """
    if bpy.context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")

    view_objects = bpy.context.view_layer.objects
    previously_selected = [obj for obj in view_objects if obj.select_get()]
    previously_active = view_objects.active

    for obj in view_objects:
        obj.select_set(False)

    selected = 0
    for obj in objects:
        try:
            obj.select_set(True)
            selected += 1
        except RuntimeError:
            pass                              # excluded from the view layer

    if not selected:
        raise RuntimeError("collection has no selectable meshes "
                           "(excluded from view layer?)")

    try:
        if hasattr(bpy.ops.wm, "stl_export"):             # Blender 4.1+
            options = dict(filepath=path, export_selected_objects=True,
                           apply_modifiers=True, global_scale=scale,
                           forward_axis="Y", up_axis="Z", ascii_format=False)
            try:
                bpy.ops.wm.stl_export(**options)
            except TypeError:
                options.pop("ascii_format", None)
                bpy.ops.wm.stl_export(**options)
        else:                                             # Blender 4.0 and older
            bpy.ops.export_mesh.stl(
                filepath=path, use_selection=True, use_mesh_modifiers=True,
                global_scale=scale, axis_forward="Y", axis_up="Z", ascii=False)
    finally:
        for obj in view_objects:
            obj.select_set(False)
        for obj in previously_selected:
            try:
                obj.select_set(True)
            except RuntimeError:
                pass
        view_objects.active = previously_active


# ---------------------------------------------------------------------------
# G-code header repair
# ---------------------------------------------------------------------------

def scan_gcode(lines):
    """Replay the toolpath to recover what CuraEngine's CLI leaves out.

    Returns (print_seconds, filament_mm, flow_samples, bounds) where flow_samples
    holds extruded millimetres per millimetre travelled, and bounds is
    (min_x, min_y, min_z, max_x, max_y, max_z).
    """
    x = y = z = 0.0
    extruder = 0.0
    filament_mm = 0.0
    print_seconds = 0.0
    flow_samples = []
    low = [float("inf")] * 3
    high = [float("-inf")] * 3
    printing = False

    for line in lines:
        if line.startswith(";LAYER:"):
            printing = True                   # everything before this is start gcode
        elif line.startswith(";TIME_ELAPSED:"):
            print_seconds = float(line.split(":")[1])

        words = line.split(";")[0].split()
        if not words:
            continue

        if words[0] == "G92":                 # extruder position reset
            for word in words[1:]:
                if word.startswith("E"):
                    extruder = float(word[1:])
            continue

        if words[0] not in ("G0", "G1"):
            continue

        next_x, next_y, extrude_to = x, y, None
        for word in words[1:]:
            axis, value = word[0], word[1:]
            if axis == "X":
                next_x = float(value)
            elif axis == "Y":
                next_y = float(value)
            elif axis == "Z":
                z = float(value)
            elif axis == "E":
                extrude_to = float(value)

        travelled = math.hypot(next_x - x, next_y - y)

        if extrude_to is not None:
            extruded, extruder = extrude_to - extruder, extrude_to
            if printing and extruded > 0 and travelled > 1e-9:
                filament_mm += extruded
                if travelled > 0.5:           # long moves give a clean flow reading
                    flow_samples.append(extruded / travelled)
                low = [min(low[0], next_x), min(low[1], next_y), min(low[2], z)]
                high = [max(high[0], next_x), max(high[1], next_y), max(high[2], z)]

        x, y = next_x, next_y

    return print_seconds, filament_mm, flow_samples, low + high


def repair_gcode_header(gcode_path, definition, overrides):
    """Fill in the header the CLI leaves blank and sanity check the flow.

    Returns a warning string, or None when everything looks right.
    """
    def setting(key, fallback):
        if key in overrides:
            return overrides[key]
        return definition.get("overrides", {}).get(key, {}).get("default_value", fallback)

    layer_height = float(setting("layer_height", 0.2))
    line_width = float(setting("line_width", 0.4))
    filament_diameter = float(setting("material_diameter", 1.75))
    filament_area = math.pi * (filament_diameter / 2.0) ** 2

    with open(gcode_path, "r", encoding="utf-8", errors="ignore", newline="") as handle:
        lines = handle.readlines()

    print_seconds, filament_mm, flow_samples, bounds = scan_gcode(lines)
    if not flow_samples:
        return "no extrusion in output"

    # Width the printer will actually lay down. If it disagrees with the profile,
    # material_diameter most likely never reached the extruder train.
    median_flow = sorted(flow_samples)[len(flow_samples) // 2]
    effective_width = median_flow * filament_area / layer_height

    warning = None
    if abs(effective_width - line_width) / line_width > 0.10:
        warning = ("FLOW WRONG: line width %.3f mm, expected %.3f (%.0f%%)"
                   % (effective_width, line_width, 100.0 * effective_width / line_width))
        print("[slice] !! " + warning)
        print("[slice] !! check material_diameter reaching the extruder train")
    else:
        print("[slice] flow ok: effective line width %.3f mm" % effective_width)

    header = {
        ";TIME:": "%d" % int(print_seconds),
        ";Filament used: ": "%.5fm" % (filament_mm / 1000.0),
        ";Layer height: ": "%g" % layer_height,
        ";MINX:": "%g" % bounds[0], ";MINY:": "%g" % bounds[1], ";MINZ:": "%g" % bounds[2],
        ";MAXX:": "%g" % bounds[3], ";MAXY:": "%g" % bounds[4], ";MAXZ:": "%g" % bounds[5],
    }
    line_ending = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"

    for index, line in enumerate(lines[:20]):
        for tag, value in header.items():
            if line.startswith(tag):
                lines[index] = tag + value + line_ending

    with open(gcode_path, "w", encoding="utf-8", newline="") as handle:
        handle.writelines(lines)
    return warning


def read_gcode_summary(gcode_path):
    """Pull the interesting header comments back out for the console report."""
    wanted = ("TIME:", "Filament used:", "LAYER_COUNT:", "Layer height:")
    summary = {}

    with open(gcode_path, "r", encoding="utf-8", errors="ignore") as handle:
        for _ in range(300):
            line = handle.readline()
            if not line:
                break
            if not line.startswith(";"):
                continue
            for tag in wanted:
                if line[1:].startswith(tag):
                    summary[tag.rstrip(":")] = line[1 + len(tag):].strip()
            if "LAYER_COUNT" in summary:
                break
    return summary


# ---------------------------------------------------------------------------
# Slicing
# ---------------------------------------------------------------------------

def run_cura_engine(engine, printer_definition, stl_path, gcode_path,
                    overrides, definitions_dir, workdir, verbose):
    """Run the slicer and return how long it took, in seconds."""
    command = [engine, "slice"]
    if verbose:
        command.append("-v")
    command += ["-j", printer_definition]
    for key, value in overrides.items():
        command += ["-s", "%s=%s" % (key, value)]
    command += ["-o", gcode_path, "-l", stl_path, "-s", "center_object=true"]

    environment = dict(os.environ)
    environment["CURA_ENGINE_SEARCH_PATH"] = definitions_dir + os.pathsep + workdir

    print("[slice] " + subprocess.list2cmdline(command))
    started_at = time.time()
    result = subprocess.run(
        command, capture_output=True, text=True, env=environment, errors="ignore",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    elapsed = time.time() - started_at

    log = (result.stdout or "") + (result.stderr or "")
    wrote_gcode = os.path.isfile(gcode_path) and os.path.getsize(gcode_path) >= 512
    if result.returncode != 0 or not wrote_gcode:
        print(log[-4000:])
        raise RuntimeError("CuraEngine failed (exit %s) - see system console"
                           % result.returncode)

    for line in log.splitlines():
        if "[WARNING]" in line or "[ERROR]" in line:
            print("[slice] " + line.strip())
    return elapsed


def print_report(gcode_path, slice_seconds):
    summary = read_gcode_summary(gcode_path)
    minutes = int(float(summary.get("TIME", 0))) // 60
    print("[slice] gcode-> %s" % gcode_path)
    print("[slice] %s layers @ %s mm | print %dh %02dm | %s | sliced in %.1fs" % (
        summary.get("LAYER_COUNT", "?"), summary.get("Layer height", "?"),
        minutes // 60, minutes % 60, summary.get("Filament used", "?"), slice_seconds))


def slice_active_collection(profile_index=-1):
    """Export the active collection and slice it next to the .blend file.

    Returns (gcode_path, warning, profile_name).
    """
    preferences = get_preferences()
    if not bpy.data.filepath:
        raise RuntimeError("save the .blend first - output goes to the project folder")

    index, profile = get_profile(profile_index)

    cura_root = absolute_path(preferences.cura_root)
    if not os.path.isdir(cura_root):
        raise RuntimeError("Cura folder not found: " + cura_root)

    definition_json = absolute_path(profile.path)
    if not os.path.isfile(definition_json):
        raise RuntimeError("profile %r has no valid .json - fix it in the "
                           "add-on preferences" % profile.name)

    collection, meshes = active_collection_meshes()
    if not meshes:
        raise RuntimeError("collection %r has no meshes" % collection.name)

    overrides = parse_overrides(preferences.overrides, profile.overrides)
    project_dir = os.path.dirname(bpy.data.filepath)
    stem = output_stem(preferences, collection) + sanitize_filename(profile.suffix)
    stl_path = os.path.join(project_dir, stem + ".stl")
    gcode_path = os.path.join(project_dir, stem + ".gcode")

    print("[slice] profile %r -> %s" % (profile.name, os.path.basename(definition_json)))
    print("[slice] collection %r - %d mesh(es)" % (collection.name, len(meshes)))
    size = bounding_box_mm(meshes, preferences.scale)
    if size:
        print("[slice] model %.1f x %.1f x %.1f mm" % size)

    export_stl(stl_path, meshes, preferences.scale)
    print("[slice] stl  -> %s (%.1f KB)" % (stl_path, os.path.getsize(stl_path) / 1024))

    definitions_dir = find_definitions_dir(cura_root)
    workdir, printer_definition, definition = build_printer_definition(
        definition_json, definitions_dir, profile.name)

    slice_seconds = run_cura_engine(
        find_cura_engine(cura_root), printer_definition, stl_path, gcode_path,
        overrides, definitions_dir, workdir, preferences.verbose)

    warning = repair_gcode_header(gcode_path, definition, overrides)

    if not preferences.keep_stl:
        try:
            os.remove(stl_path)
        except OSError:
            pass

    print_report(gcode_path, slice_seconds)

    if preferences.open_folder:
        subprocess.Popen(["explorer", "/select,", os.path.normpath(gcode_path)])

    preferences.last_used = index
    return gcode_path, warning, profile.name


# ---------------------------------------------------------------------------
# Operators and menus
# ---------------------------------------------------------------------------

class WM_OT_quick_slice(bpy.types.Operator):
    bl_idname = "wm.quick_slice"
    bl_label = "Quick Slice"
    bl_description = "Export the active collection and slice it with CuraEngine"

    profile_index: IntProperty(default=-1, options={"SKIP_SAVE"})

    def execute(self, context):
        try:
            path, warning, profile_name = slice_active_collection(self.profile_index)
        except Exception as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}

        if warning:
            self.report({"WARNING"}, warning)
        else:
            self.report({"INFO"}, "%s -> %s" % (profile_name, os.path.basename(path)))
        return {"FINISHED"}


class QUICKSLICE_MT_profiles(bpy.types.Menu):
    bl_idname = "QUICKSLICE_MT_profiles"
    bl_label = "Quick Slice"

    def draw(self, context):
        layout = self.layout
        profiles = get_preferences().profiles

        if not len(profiles):
            layout.label(text="no profiles - add them in preferences", icon="ERROR")
            return

        for index, profile in enumerate(profiles):
            label = "%d  %s" % (index + 1, profile.name) if index < 9 else profile.name
            entry = layout.operator(WM_OT_quick_slice.bl_idname, text=label)
            entry.profile_index = index


class WM_OT_quick_slice_menu(bpy.types.Operator):
    bl_idname = "wm.quick_slice_menu"
    bl_label = "Quick Slice (pick profile)"
    bl_description = "Choose a slicing profile, then slice the active collection"

    def execute(self, context):
        if len(get_preferences().profiles) <= 1:
            return bpy.ops.wm.quick_slice(profile_index=0)
        bpy.ops.wm.call_menu(name=QUICKSLICE_MT_profiles.bl_idname)
        return {"FINISHED"}


class QUICKSLICE_OT_profile_add(bpy.types.Operator):
    bl_idname = "quickslice.profile_add"
    bl_label = "Add Profile"

    def execute(self, context):
        profiles = get_preferences().profiles
        profiles.add().name = "profile %d" % (len(profiles) + 1)
        return {"FINISHED"}


class QUICKSLICE_OT_profile_remove(bpy.types.Operator):
    bl_idname = "quickslice.profile_remove"
    bl_label = "Remove Profile"

    index: IntProperty(default=-1)

    def execute(self, context):
        preferences = get_preferences()
        if 0 <= self.index < len(preferences.profiles):
            preferences.profiles.remove(self.index)
            preferences.last_used = min(preferences.last_used,
                                        max(len(preferences.profiles) - 1, 0))
        return {"FINISHED"}


class QUICKSLICE_OT_profile_move(bpy.types.Operator):
    bl_idname = "quickslice.profile_move"
    bl_label = "Move Profile"

    index: IntProperty(default=-1)
    step: IntProperty(default=1)

    def execute(self, context):
        preferences = get_preferences()
        profiles = preferences.profiles
        destination = self.index + self.step

        if 0 <= self.index < len(profiles) and 0 <= destination < len(profiles):
            profiles.move(self.index, destination)
            if preferences.last_used == self.index:
                preferences.last_used = destination
            elif preferences.last_used == destination:
                preferences.last_used = self.index
        return {"FINISHED"}


def draw_export_menu(self, context):
    self.layout.menu(QUICKSLICE_MT_profiles.bl_idname, icon="MOD_SOLIDIFY")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

CLASSES = (
    QuickSliceProfile,
    QUICKSLICE_OT_profile_add,
    QUICKSLICE_OT_profile_remove,
    QUICKSLICE_OT_profile_move,
    QuickSlicePreferences,
    WM_OT_quick_slice,
    QUICKSLICE_MT_profiles,
    WM_OT_quick_slice_menu,
)

keymap_entries = []


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_export.append(draw_export_menu)

    keyconfig = bpy.context.window_manager.keyconfigs.addon
    if keyconfig:
        keymap = keyconfig.keymaps.new(name="Window")

        # F6 picks a profile, Shift+F6 repeats the last one.
        pick = keymap.keymap_items.new(WM_OT_quick_slice_menu.bl_idname, "F6", "PRESS")
        keymap_entries.append((keymap, pick))

        repeat = keymap.keymap_items.new(WM_OT_quick_slice.bl_idname, "F6", "PRESS",
                                         shift=True)
        repeat.properties.profile_index = -1
        keymap_entries.append((keymap, repeat))


def unregister():
    for keymap, entry in keymap_entries:
        try:
            keymap.keymap_items.remove(entry)
        except Exception:
            pass
    keymap_entries.clear()

    bpy.types.TOPBAR_MT_file_export.remove(draw_export_menu)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()