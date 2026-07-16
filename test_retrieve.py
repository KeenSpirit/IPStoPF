
def test(app):
# 1. Get relay types from ErgonLibrary
    global_library = app.GetGlobalLibrary()
    protection_lib = global_library.GetContents("Protection")
    app.PrintPlain(f"protection_lib: {protection_lib}")
    ergon_types = all_relevant_objects(app, protection_lib, "*.TypRelay", None)
    app.PrintPlain(f"ergon_types: {ergon_types}")
    index = {}
    for relay_type in ergon_types or []:
        name = relay_type.loc_name
        index[name] = relay_type

        app.PrintPlain(
            f"Type index: Ergon library fetched "
            f"({len(index)} candidate types)"
        )

    # 2. Get relay types from DIgSILENT library
    try:
        database = global_library.fold_id
        app.PrintPlain(f"database: {database}")
        dig_lib = database.GetContents("Lib")[0]
        app.PrintPlain(f"dig_lib: {dig_lib}")
        prot_lib = dig_lib.GetContents("Prot")[0]
        app.PrintPlain(f"prot_lib: {prot_lib}")
        relay_lib = prot_lib.GetContents("ProtRelay")
        app.PrintPlain(f"relay_lib: {relay_lib}")
        # Server-side recursive fetch: one round-trip per ProtRelay folder
        # instead of one per subfolder. The Python-level crawl
        # (all_relevant_objects) took ~60 s co-located and 70+ min over
        # WAN latency (Tablelands, 2026-07-15).
        dig_types = []
        for _folder in relay_lib or []:
            dig_types.extend(_folder.GetContents("*.TypRelay", 1) or [])
        app.PrintPlain(
            f"Type index: DIgSILENT library fetched "
            f"({len(dig_types)} candidate types)"
        )
    except (IndexError, AttributeError):
        pass


def all_relevant_objects(
        app,
        folders,
        type_of_obj,
        objects):
    """
    Recursively retrieve all objects of a given type from folder hierarchy.

    This function performs a depth-first traversal of the folder structure,
    collecting all objects matching the specified type. It's faster than
    using GetContents with recursive=True on folders outside your own user.

    Args:
        app: PowerFactory application object
        folders: List of folder objects to search
        type_of_obj: Object type pattern (e.g., "*.ElmRelay", "*.RelFuse")
        objects: Accumulator for recursive calls (internal use)

    Returns:
        List of all matching PowerFactory objects

    Example:
        >>> net_mod = app.GetProjectFolder("netmod")
        >>> relays = all_relevant_objects(app, [net_mod], "*.ElmRelay")
    """
    if objects is None:
        objects = []

    for folder in folders:
        # Get objects at this level (non-recursive)
        folder_objects = folder.GetContents(type_of_obj, 0)
        objects.extend(folder_objects)

        # Get subfolders to recurse into
        sub_folders = folder.GetContents("*.IntFolder", 0)
        sub_folders += folder.GetContents("*.IntPrjfolder", 0)

        if sub_folders:
            all_relevant_objects(app, sub_folders, type_of_obj, objects)

    return objects