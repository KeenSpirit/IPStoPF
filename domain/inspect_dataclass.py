from domain import sub_dataclass as dc

def dump_substation(app, ss: dc.Site) -> None:
    """Print the full contents of a Substation as an indented tree."""
    app.PrintPlain(f"Substation: {ss.name}")
    for kv in sorted(ss.voltage_levels):
        vl = ss.voltage_levels[kv]
        app.PrintPlain(f"  {kv} kV")
        for etype, by_name in vl.elements.items():
            app.PrintPlain(f"    {etype.value}")
            for el in by_name.values():
                rc = el.relay_cubicle
                model = f", {rc.relay_model}" if rc.relay_model else ""
                obj = "" if el.obj is None else f"  obj={el.obj!r}"
                app.PrintPlain(f"      {el.obj}  [cubicle: {rc.obj}{model}]{obj}")