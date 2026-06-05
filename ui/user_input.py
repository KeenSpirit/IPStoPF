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