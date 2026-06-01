"""

"""

import time
import powerfactory as pf
from user_interface import user_input as ui, obtain_all_grids as oag
from process_pf_elements import process_elements as pe
from mapping import reconcile, pf_source
from process_ips import ips_ingest as ii
from config import paths

from importlib import reload

reload(ui)
reload(pe)

def run_main():

    start = time.time()
    app = pf.GetApplication()
    app.ClearOutputWindow()
    # Enables the user to manually stop the script
    app.SetEnableUserBreak(1)
    app.ResetCalculation()
    # Turn the echo off (suppress output window messages)
    echo(app)

    ips_export = paths.get_ips_data()
    ips = ii.ingest_ips_export(str(ips_export))

    exg_grids_sorted = oag.all_egx_grids(app)
    selected_grid = ui.select_object(exg_grids_sorted)
    sites = pe.process_elements(app, selected_grid)
    pf_result = pf_source.pf_refs_from_sites(sites)
    result = reconcile(ips.by_key, pf_result)
    app.PrintPlain(result)

    # Restore the echo
    echo(app, off=False)
    app.PrintPlain(f'Script finished')
    end = time.time()
    run_time = round(end - start, 6)
    run_time = format_time(run_time)
    app.PrintPlain(f"Script run time: {run_time}")


def format_time(seconds):
    hours, remainder = divmod(int(seconds), 3600)
    minutes, seconds = divmod(remainder, 60)

    time_parts = []
    if hours > 0:
        time_parts.append(f"{hours} hour{'s' if hours > 1 else ''}")
    if minutes > 0:
        time_parts.append(f"{minutes} minute{'s' if minutes > 1 else ''}")
    if seconds > 0 or not time_parts:
        time_parts.append(f"{seconds} second{'s' if seconds > 1 else ''}")

    return " ".join(time_parts)


def echo(app, off=True):
    """Supresses the printing of Warning and information messages to the Output.

    Usage: Echo(app) turns the echo off
           Echo(app, off = False) turns the echo back on
    """
    echo = app.GetFromStudyCase('ComEcho')
    if off:
        echo.SetAttribute('iopt_err', True)
        echo.SetAttribute('iopt_wrng', False)
        echo.SetAttribute('iopt_info', False)
        echo.SetAttribute('iopt_oth', True)
        echo.Off()
    else:
        pass
        echo.On()


if __name__ == '__main__':

    run_main()