"""

"""
import powerfactory as pf
import os
from tkinter import *  # noqa [F403]

from ips_data import ips_settings as ips
from update_powerfactory import orchestrator as up

from config.paths import OUTPUT_BATCH_DIR, OUTPUT_LOCAL_DIR
from config.validation import (
    require_valid_config,
    validate_for_batch_mode,
    ValidationConfig,
    ValidationLevel,
)
from utils.time_utils import Timer, get_current_timestamp
from utils.file_utils import (
    ensure_directory_exists,
    get_citrix_adjusted_path,
    write_dict_list_to_csv,
    is_file_recent,
    safe_file_remove,
)
from utils import pf_utils

from ui import user_input as ui, obtain_all_grids as oag
from process_pf_elements import process_elements as pe
from mapping import reconciliation as recon, pf_source
from process_ips import ips_ingest as ii
from mapping.report import write_reconciliation_report
from config import paths
from ips_data import sbtrans_settings as ss
from ips_data import query_database as qd
from ips_data import ips_settings
from update_powerfactory import orchestrator
from core import UpdateResult

from importlib import reload

reload(ui)
reload(pe)
reload(ips_settings)
reload(orchestrator)
reload(pf_utils)

from logging_config import setup_logging, get_logger

# Initialize logging at module level after imports
setup_logging()
logger = get_logger(__name__)

def main(app=None, batch=False):
    """This Script Will be used to transfer Settings from IPS to PF."""
    timer = Timer(name="IPS to PF Transfer", auto_log=True)
    timer.start()
    start_time = get_current_timestamp()

    logger.info("IPS to PF Transfer script started")

    if not app:
        # Change called_function to True if you want to mimic a batch update
        called_function = False
        app = pf.GetApplication()
    else:
        # If another script is executing this script, it will pass the app argument to it
        called_function = True
    app.ClearOutputWindow()

    # Turn the echo off (suppress output window messages)
    echo(app)
    # Enables the user to manually stop the script
    app.SetEnableUserBreak(1)

    try:
        # ======================================================================
        # CONFIGURATION VALIDATION
        # ======================================================================
        # Validate configuration before doing anything else.
        # This catches issues early with clear error messages rather than
        # failing mid-run with cryptic stack traces.

        if batch or called_function:
            # Batch mode: stricter validation, check database connectivity
            result = validate_for_batch_mode(app)
            if not result.is_valid:
                app.PrintError("Configuration validation failed for batch mode")
                for error in result.errors:
                    app.PrintError(f"  {error}")
                logger.error(f"Configuration validation failed: {result.errors}")
                return None
            # Print warnings but continue
            for warning in result.warnings:
                app.PrintWarn(warning)
        else:
            # Interactive mode: standard validation, faster startup
            # require_valid_config() will exit automatically if invalid
            require_valid_config(app)

        # ======================================================================
        # MAIN PROCESSING
        # ======================================================================

        # Determine which IPS database is to be queried
        prjt = app.GetActiveProject()
        if prjt is None:
            app.PrintError("No active project selected. Activate a project to use this script")
            logger.error("Script terminated because no active project was selected")
            exit()
        region = pf_utils.determine_region(prjt)

        if region == "Subtransmission":
            selected_region = ui.select_region()

            if selected_region == "Energex":
                # Obtain the IPS setting-ID data from the database (corporate cache query).
                ips_records = qd.get_setting_id_records(app, ss.REGION)
                ips = ii.ingest_ips_records(ips_records)

                exg_grids_sorted = oag.all_egx_grids(app)
                while True:
                    selected_grid = ui.select_object(exg_grids_sorted)
                    sites = []
                    sites.extend(pe.process_elements(app, selected_grid))
                    pf_result = pf_source.pf_refs_from_sites(sites)
                    pf_result = ui.select_pf_elements(pf_result)
                    if pf_result is ui.GO_BACK:
                        continue
                    break
                result = recon.reconcile(ips.by_key, pf_result)
                # app.PrintPlain(result.coverage_summary())
                # report_path = write_reconciliation_report(result, paths.get_output_directory())
                # app.PrintPlain(f"Reconciliation report written to: {report_path}")

                # --- apply matched settings to PowerFactory -------------------
                set_ids, device_list = ss.build_devices_from_reconciliation(app, result)
                app.PrintPlain(f"Built {len(device_list)} devices from {len(set_ids)} setting IDs")

                data_capture_list: list[UpdateResult] = []
            else:
                batch = True
                region = "Ergon"    # This enables the inactive-record filter in _should_skip_record.
                ee_grids = oag.regional_grid(app, selected_region)
                grid = ui.select_object(ee_grids)
                device_list, data_capture_list = ips_settings.get_ips_settings(app, region, batch, called_function, grid)
        else:
            # Distribution model
            # Query the IPS data
            grid = None
            device_list, data_capture_list = ips_settings.get_ips_settings(app, region, batch, called_function, grid)

            logger.info(f"Devices found in IPS: {len(device_list)}")

        # Update PowerFactory
        data_capture_list, has_updates = up.update_pf(app, device_list, data_capture_list)

        logger.info(f"Data capture list entries: {len(data_capture_list)}")
        logger.info(f"Data capture list: {config_log_result(data_capture_list)}")
        logger.info(f"Updates applied: {has_updates}")

        # Create file to save script information
        save_file = create_save_file(app, prjt, called_function)
        if not save_file:
            return
        write_dict_list_to_csv(data_capture_list, save_file)

        # Restore the echo so the outcome messages below are visible.
        echo(app, off=False)
        #if not batch:
        print_results(app, data_capture_list)

        stop_time = get_current_timestamp()
        app.PrintInfo(
            f"Script started at {start_time} and finished at {stop_time}"
        )
        if has_updates:
            app.PrintInfo("Of the devices selected there were updated settings")
            logger.info("Script completed with updated settings")
        else:
            app.PrintInfo("Of the devices selected there were no updated settings")
            logger.info("Script completed with no updated settings")

        return has_updates
    finally:
        # Always restore the echo and stop the timer, even on early return
        # (e.g. batch "already studied"), on exit(), or on an exception.
        # Restoring echo first ensures anything emitted here is visible;
        # calling echo(off=False) a second time is harmless.
        echo(app, off=False)
        timer.stop()
        app.PrintPlain(f"Query Script run time: {timer.formatted}")


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


def create_save_file(app, prjt, called_function):
    project_name = prjt.GetAttribute("loc_name")
    current_user = app.GetCurrentUser()
    current_user_name = current_user.GetAttribute("loc_name")
    parent_folder = prjt.GetAttribute("fold_id")
    parent_folder_name = parent_folder.GetAttribute("loc_name")
    file_name = f"{current_user_name}_{parent_folder_name}_{project_name}".replace(
        "/", "_"
    )
    if called_function:
        file_location = OUTPUT_BATCH_DIR
        main_file_name = select_main_file(
            file_name, file_location, called_function
        )
    else:
        file_location = OUTPUT_LOCAL_DIR
        main_file_name = select_main_file(
            file_name, file_location, called_function
        )
    return main_file_name


def select_main_file(file_name, location, called_function):
    """Check to see if a folder structure exists and create it if it doesn't.
    Create the csv file to publish all the data."""
    # Adjust path for Citrix environment
    location = get_citrix_adjusted_path(location)

    # Ensure directory exists
    ensure_directory_exists(location)

    set_file_name = os.path.join(location, f"{file_name}.csv")
    print(set_file_name)

    # Check if file was recently modified
    if called_function:
        # For batch mode, skip if file was modified in last 24 hours
        if is_file_recent(set_file_name, max_age_seconds=24 * 60 * 60):
            print("Project had already been studied")
            return None

    # Remove existing file if present
    safe_file_remove(set_file_name)

    return set_file_name


def print_results(app, data_capture_list):
    """This is to provide information to the user about the results of the
    script."""
    devices, device_object_dict = pf_utils.get_all_protection_devices(app)
    print_string = str()
    # app.ClearOutputWindow()
    for i, info in enumerate(data_capture_list):
        if i % 40 == 0:
            app.PrintInfo(print_string)
            print_string = str()
        try:
            device = info["PLANT_NUMBER"]
            pf_device = device_object_dict[device][0]
        except KeyError:
            try:
                pf_device = info["CB_NAME"]
            except KeyError:
                pf_device = info["PLANT_NUMBER"]
        try:
            result = info["RESULT"]
        except KeyError:
            result = "Updated Successfully"
        print_string = print_string + "\n" + f"{pf_device}    Result = {result}"
    app.PrintInfo(print_string)


def config_log_result(data_capture_list):
    """
    Only log results of interest
    :param data_capture_list:
    :return:
    """

    log_results = []

    for info in data_capture_list:
        log_result = {'SUBSTATION': info['SUBSTATION']}
        try:
            device_name = info["PLANT_NUMBER"]
        except KeyError:
            device_name = info["CB_NAME"]
        log_result["DEVICE NAME"] = device_name
        try:
            log_result["RESULT"] = info["RESULT"]
        except KeyError:
            pass
        log_results.append(log_result)
    return log_results


if __name__ == '__main__':
    # Logging is already configured via setup_logging() at module level
    updates_applied = main()