import tkinter as tk
from tkinter import messagebox
import sys
import math


def select_object(objects):
    """
    Present a list of objects to the user and return the single object selected.

    Each object must expose a `loc_name` string attribute.

    Sizing behaviour:
        - The window grows tall enough to show every item, using the fewest
          columns (up to 3) needed.
        - Window height is capped so it always stays on screen.
        - A vertical scrollbar is added ONLY if, even at 3 columns and maximum
          on-screen height, the items still don't all fit.

    Returns:
        The object the user selected and confirmed with "Okay".
        "Exit Application" (or closing the window) terminates the program.
    """
    if not objects:
        raise ValueError("The list of objects is empty - nothing to select.")

    root = tk.Tk()
    root.title("IPS to PowerFactory - Sub-transmission")

    result = {"object": None}  # holder so nested callbacks can write back

    # ---- Heading / instruction text ------------------------------------
    header = tk.Label(
        root,
        text="Select Bulk Supply Grid to Update:",
        font=("Segoe UI", 11, "bold"),
        anchor="w",
        padx=12,
        pady=10,
    )
    header.pack(side="top", fill="x")

    # ---- Reserve the bottom button bar (fixed, never scrolls) ----------
    button_bar = tk.Frame(root)
    button_bar.pack(side="bottom", fill="x", padx=12, pady=10)

    # ---- Scrollable body -----------------------------------------------
    body = tk.Frame(root)
    body.pack(side="top", fill="both", expand=True, padx=12)

    canvas = tk.Canvas(body, highlightthickness=0)
    inner = tk.Frame(canvas)
    canvas.create_window((0, 0), window=inner, anchor="nw")

    # ---- Radio buttons (created once, re-laid-out while choosing cols) --
    selected = tk.IntVar(value=-1)   # -1 means "nothing chosen yet"
    radios = [
        tk.Radiobutton(inner, text=obj.loc_name, variable=selected,
                       value=i, anchor="w", padx=6)
        for i, obj in enumerate(objects)
    ]
    n = len(radios)

    def lay_out(cols):
        """Place every radio button in `cols` columns, filled top-to-bottom."""
        rows_per_col = math.ceil(n / cols)
        for i, rb in enumerate(radios):
            rb.grid(row=i % rows_per_col, column=i // rows_per_col,
                    sticky="w", padx=6, pady=1)
        return rows_per_col

    # ---- Work out how much vertical room the list actually has ---------
    root.update_idletasks()
    screen_h = root.winfo_screenheight()
    chrome_h = (header.winfo_reqheight()      # heading
                + button_bar.winfo_reqheight()  # button bar
                + 40)                           # pack padding allowance
    title_bar_allowance = 90                    # title bar + taskbar + margin
    max_content_h = max(150, screen_h - chrome_h - title_bar_allowance)

    # ---- Pick the fewest columns (up to 3) whose content fits ----------
    chosen_cols = 3
    need_scrollbar = True
    for c in (1, 2, 3):
        lay_out(c)
        inner.update_idletasks()
        if inner.winfo_reqheight() <= max_content_h:
            chosen_cols = c
            need_scrollbar = False
            break

    if need_scrollbar:          # 3 columns still too tall -> use a scrollbar
        lay_out(3)
        inner.update_idletasks()

    # ---- Size the canvas to the real content (capped to screen) --------
    content_h = inner.winfo_reqheight()
    content_w = inner.winfo_reqwidth()
    canvas_h = max_content_h if need_scrollbar else content_h
    canvas.configure(width=content_w, height=canvas_h)

    if need_scrollbar:
        vbar = tk.Scrollbar(body, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vbar.set)
        vbar.pack(side="right", fill="y")

        def _wheel(event):           # Windows / macOS
            if event.delta:
                canvas.yview_scroll(int(-event.delta / 120), "units")

        def _wheel_linux(event):     # X11
            canvas.yview_scroll(-1 if event.num == 4 else 1, "units")

        canvas.bind_all("<MouseWheel>", _wheel)
        canvas.bind_all("<Button-4>", _wheel_linux)
        canvas.bind_all("<Button-5>", _wheel_linux)

    canvas.pack(side="left", fill="both", expand=True)
    inner.bind("<Configure>",
               lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

    # ---- Button callbacks ----------------------------------------------
    def on_okay():
        idx = selected.get()
        if idx < 0:
            messagebox.showwarning(
                "No selection",
                "Please make a selection before proceeding.",
                parent=root,
            )
            return
        result["object"] = objects[idx]
        root.destroy()

    def on_exit():
        root.destroy()
        sys.exit(0)

    tk.Button(button_bar, text="Okay", width=12,
              command=on_okay).pack(side="left")
    tk.Button(button_bar, text="Exit Application", width=14,
              command=on_exit).pack(side="left", padx=(8, 0))

    root.protocol("WM_DELETE_WINDOW", on_exit)   # X button behaves like Exit

    # ---- Centre on screen ----------------------------------------------
    root.update_idletasks()
    w = root.winfo_reqwidth()
    h = min(root.winfo_reqheight(), screen_h - title_bar_allowance)
    x = (root.winfo_screenwidth() - w) // 2
    y = max(0, (screen_h - h) // 3)              # slightly above dead centre
    root.geometry(f"{w}x{h}+{x}+{y}")
    root.minsize(w, min(h, 200))

    root.mainloop()
    return result["object"]


# =============================================================================
# Element selection (added)
# =============================================================================
#

# Per-element processing budget used for the time estimate (seconds).
_SECONDS_PER_ELEMENT = 30


def _fmt_voltage(v) -> str:
    """Render a MappingKey voltage for display ('132 kV', '11 kV', 'LV')."""
    if isinstance(v, bool):                       # guard: bool is a subclass of int
        return str(v)
    if isinstance(v, (int, float)):
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        return f"{v} kV"
    return str(v)                                 # e.g. the literal "LV"


def _voltage_sort_key(v):
    """Sort voltages high-to-low; non-numeric voltages (e.g. 'LV') go last."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return (0, -float(v), "")
    return (1, 0.0, str(v))


def _fmt_duration(seconds: int) -> str:
    """Format a whole-second duration as 'Hh Mm Ss', dropping leading zero
    groups but always keeping at least the seconds field (e.g. '1h 30m 0s',
    '5m 30s', '0s')."""
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes or parts:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def select_pf_elements(pf_result):
    """
    Present every processed PowerFactory element (the ``PfSourceResult``
    produced by ``pf_source.pf_refs_from_sites``) as a tree of nested
    checkboxes and return a new ``PfSourceResult`` containing only the
    elements the user selected.

    The tree is a single scrolling column grouped substation -> voltage level
    -> element. Ticking a voltage box ticks every element under it; ticking a
    substation box ticks every voltage box and element box under it. A parent
    box shows the tri-state ("some selected") indicator when only part of its
    subtree is ticked.

    Window behaviour mirrors ``select_object``: it grows to fit the content,
    is capped to the screen height, and only then grows a vertical scrollbar.
    The bottom bar is always visible and holds, left to right, "Select All",
    "Okay" and "Exit Application", with a live "Estimated model update time"
    label pinned to the right (number of ticked elements x 30 s).

    Returns:
        A ``PfSourceResult`` whose ``refs`` are the selected elements (its
        ``skipped`` list is carried through unchanged). "Exit Application" (or
        closing the window) terminates the program, matching select_object.
    """
    # Local import keeps ui decoupled at module load time and avoids any
    # import-order surprises; mapping.pf_source does not import ui.
    from mapping.pf_source import PfSourceResult

    refs = list(pf_result.refs)
    if not refs:
        raise ValueError("pf_result contains no elements to select.")

    # ---- Group refs: site -> voltage -> [refs] -------------------------
    grouped = {}
    for ref in refs:
        site = ref.key.site_code
        volt = ref.key.voltage_kv
        grouped.setdefault(site, {}).setdefault(volt, []).append(ref)

    root = tk.Tk()
    root.title("IPS to PowerFactory - Sub-transmission")

    result = {"refs": None}   # holder so nested callbacks can write back

    # ---- Heading -------------------------------------------------------
    header = tk.Label(
        root,
        text="Select elements to match with IPS patterns:",
        font=("Segoe UI", 11, "bold"),
        anchor="w", padx=12, pady=10,
    )
    header.pack(side="top", fill="x")

    # ---- Bottom button bar (fixed, never scrolls) ----------------------
    button_bar = tk.Frame(root)
    button_bar.pack(side="bottom", fill="x", padx=12, pady=10)

    # ---- Scrollable body -----------------------------------------------
    body = tk.Frame(root)
    body.pack(side="top", fill="both", expand=True, padx=12)

    canvas = tk.Canvas(body, highlightthickness=0)
    inner = tk.Frame(canvas)
    canvas.create_window((0, 0), window=inner, anchor="nw")

    # ---- Tree model ----------------------------------------------------
    # A node is a dict. Leaves carry the ref + a BooleanVar; group nodes
    # (substation, voltage) carry a tri-state StringVar and a children list.
    leaves = []          # every leaf node, for counting / select-all
    substations = []     # top-level group nodes, in display order

    def _leaf_iter(node):
        if node["kind"] == "leaf":
            yield node
        else:
            for c in node["children"]:
                yield from _leaf_iter(c)

    def _set_subtree(node, on):
        if node["kind"] == "leaf":
            node["var"].set(on)
        else:
            node["var"].set("on" if on else "off")
            for c in node["children"]:
                _set_subtree(c, on)

    def _refresh_group(node):
        sub_leaves = list(_leaf_iter(node))
        n_on = sum(1 for l in sub_leaves if l["var"].get())
        if n_on == 0:
            node["var"].set("off")
        elif n_on == len(sub_leaves):
            node["var"].set("on")
        else:
            node["var"].set("tri")

    def update_estimate():
        n = sum(1 for l in leaves if l["var"].get())
        est_label.config(
            text=f"Estimated model update time: "
                 f"{_fmt_duration(n * _SECONDS_PER_ELEMENT)}"
        )

    def on_leaf(node):
        # var already toggled by tkinter; reflect upward then re-estimate.
        p = node["parent"]
        while p is not None:
            _refresh_group(p)
            p = p["parent"]
        update_estimate()

    def on_group(node):
        # On click tkinter sets the var to onvalue/offvalue (tri -> on).
        on = (node["var"].get() == "on")
        _set_subtree(node, on)
        p = node["parent"]
        while p is not None:
            _refresh_group(p)
            p = p["parent"]
        update_estimate()

    # ---- Build the widget tree (single column, indented) ---------------
    INDENT_SUB, INDENT_VOLT, INDENT_ELEM = 6, 30, 56

    for site in sorted(grouped):
        sub_node = {"kind": "group", "parent": None, "children": [],
                    "var": tk.StringVar(value="off")}
        substations.append(sub_node)
        sub_cb = tk.Checkbutton(
            inner, text=site, variable=sub_node["var"],
            onvalue="on", offvalue="off", tristatevalue="tri",
            anchor="w", padx=6, font=("Segoe UI", 10, "bold"),
            command=lambda n=sub_node: on_group(n),
        )
        sub_cb.pack(anchor="w", padx=(INDENT_SUB, 0), pady=(4, 0))

        for volt in sorted(grouped[site], key=_voltage_sort_key):
            volt_node = {"kind": "group", "parent": sub_node, "children": [],
                         "var": tk.StringVar(value="off")}
            sub_node["children"].append(volt_node)
            volt_cb = tk.Checkbutton(
                inner, text=_fmt_voltage(volt), variable=volt_node["var"],
                onvalue="on", offvalue="off", tristatevalue="tri",
                anchor="w", padx=6, font=("Segoe UI", 10),
                command=lambda n=volt_node: on_group(n),
            )
            volt_cb.pack(anchor="w", padx=(INDENT_VOLT, 0))

            for ref in sorted(grouped[site][volt], key=lambda r: r.raw_name):
                leaf_node = {"kind": "leaf", "parent": volt_node,
                             "var": tk.BooleanVar(value=False), "ref": ref}
                volt_node["children"].append(leaf_node)
                leaves.append(leaf_node)
                label = f"{ref.raw_name}  ({ref.category})"
                leaf_cb = tk.Checkbutton(
                    inner, text=label, variable=leaf_node["var"],
                    anchor="w", padx=6, font=("Segoe UI", 9),
                    command=lambda n=leaf_node: on_leaf(n),
                )
                leaf_cb.pack(anchor="w", padx=(INDENT_ELEM, 0))

    # ---- Work out how much vertical room the list has ------------------
    root.update_idletasks()
    screen_h = root.winfo_screenheight()
    chrome_h = header.winfo_reqheight() + button_bar.winfo_reqheight() + 40
    title_bar_allowance = 90
    max_content_h = max(150, screen_h - chrome_h - title_bar_allowance)

    content_h = inner.winfo_reqheight()
    content_w = inner.winfo_reqwidth()
    need_scrollbar = content_h > max_content_h
    canvas_h = max_content_h if need_scrollbar else content_h
    canvas.configure(width=content_w, height=canvas_h)

    if need_scrollbar:
        vbar = tk.Scrollbar(body, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vbar.set)
        vbar.pack(side="right", fill="y")

        def _wheel(event):           # Windows / macOS
            if event.delta:
                canvas.yview_scroll(int(-event.delta / 120), "units")

        def _wheel_linux(event):     # X11
            canvas.yview_scroll(-1 if event.num == 4 else 1, "units")

        canvas.bind_all("<MouseWheel>", _wheel)
        canvas.bind_all("<Button-4>", _wheel_linux)
        canvas.bind_all("<Button-5>", _wheel_linux)

    canvas.pack(side="left", fill="both", expand=True)
    inner.bind("<Configure>",
               lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

    # ---- Bottom bar: Select All | Okay | Exit ... estimate -------------
    select_all_state = {"will_select": True}

    def on_select_all():
        on = select_all_state["will_select"]
        for node in substations:
            _set_subtree(node, on)
        select_all_state["will_select"] = not on
        update_estimate()

    def on_okay():
        chosen = [l["ref"] for l in leaves if l["var"].get()]
        if not chosen:
            messagebox.showwarning(
                "No selection",
                "Please select at least one element before proceeding.",
                parent=root,
            )
            return
        result["refs"] = chosen
        root.destroy()

    def on_exit():
        root.destroy()
        sys.exit(0)

    tk.Button(button_bar, text="Select All", width=12,
              command=on_select_all).pack(side="left")
    tk.Button(button_bar, text="Okay", width=12,
              command=on_okay).pack(side="left", padx=(8, 0))
    tk.Button(button_bar, text="Exit Application", width=14,
              command=on_exit).pack(side="left", padx=(8, 0))

    est_label = tk.Label(button_bar, anchor="e", font=("Segoe UI", 10))
    est_label.pack(side="right")
    update_estimate()   # show the initial "0s"

    root.protocol("WM_DELETE_WINDOW", on_exit)   # X button behaves like Exit

    # ---- Centre on screen ----------------------------------------------
    root.update_idletasks()
    w = root.winfo_reqwidth()
    h = min(root.winfo_reqheight(), screen_h - title_bar_allowance)
    x = (root.winfo_screenwidth() - w) // 2
    y = max(0, (screen_h - h) // 3)
    root.geometry(f"{w}x{h}+{x}+{y}")
    root.minsize(w, min(h, 200))

    root.mainloop()

    if result["refs"] is None:        # window closed without Okay
        sys.exit(0)

    return PfSourceResult(refs=result["refs"],
                          skipped=list(pf_result.skipped))