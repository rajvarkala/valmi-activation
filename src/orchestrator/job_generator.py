import json
import logging
import os
import shutil
import threading
import time
from os.path import dirname, join
import requests
from requests.auth import HTTPBasicAuth
from dagster_graphql import ShutdownRepositoryLocationInfo, ShutdownRepositoryLocationStatus
from jinja2 import Environment, FileSystemLoader
from pydantic import Json
from vyper import v
from utils.retry_decorators import retry_on_exception, exception_to_sys_exit
from api.schemas import SyncScheduleCreate
from api.services import get_syncs_service
from metastore.session import get_session
from .dagster_client import ValmiDagsterClient

logger = logging.getLogger(v.get("LOGGER_NAME"))
GENERATED_DIR = "generated"
GENERATED_CONFIG_DIR = "config"
GENERATED_CATALOG_DIR = "catalog"
SHARED_DIR = "/tmp/shared_dir"

# TODO: clean it up with a better location
repo_ready = False


class JobCreatorThread(threading.Thread):
    def __init__(self, threadID: int, name: str, dagster_client: ValmiDagsterClient) -> None:
        threading.Thread.__init__(self)
        self.threadID = threadID
        self.exitFlag = False
        self.name = name
        self.dagster_client = dagster_client

    def run(self) -> None:
        global repo_ready
        while not self.exitFlag:
            try:
                resp = requests.get(
                    f"http://{v.get('APP_BACKEND')}:{v.get('APP_BACKEND_PORT')}/api/v1/superuser/syncs",
                    timeout=v.get("HTTP_REQ_TIMEOUT"),
                    auth=HTTPBasicAuth(v.get("ADMIN_EMAIL"), v.get("ADMIN_PASSWORD")),
                )

                # TODO: improve strategy to detect new syncs
                syncs_changed: bool = False
                try:
                    with open(join(SHARED_DIR, f"{v.get('APP')}-syncs.json"), "r") as f:
                        if (f.read()) != resp.text:
                            syncs_changed = True
                except FileNotFoundError:
                    logging.exception("syncs.json not found")
                    syncs_changed = True

                # if new syncs are found, generate dagster jobs and restart dagster repository
                if syncs_changed:
                    logger.info("New syncs found")

                    # create directories for jobs, config and catalog
                    appdir = join(SHARED_DIR, v.get("APP"))
                    if not os.path.exists(appdir):
                        os.makedirs(appdir)

                    dirs = {
                        GENERATED_DIR: join(SHARED_DIR, v.get("APP"), "gen", GENERATED_DIR),
                        GENERATED_CONFIG_DIR: join(SHARED_DIR, v.get("APP"), "gen", GENERATED_CONFIG_DIR),
                        GENERATED_CATALOG_DIR: join(SHARED_DIR, v.get("APP"), "gen", GENERATED_CATALOG_DIR),
                    }
                    for key in dirs.keys():
                        dir = dirs[key]
                        if os.path.exists(dir):
                            shutil.rmtree(dir)
                        os.makedirs(dir)

                    syncs_json = resp.json()

                    # Insert into SYNC schedules metastore
                    self.insert_syncs_into_metastore(syncs_json)

                    # generate dagster jobs
                    self.gen_dagster_job_archive(dirs, syncs_json)

                    # restart dagster repository
                    self.restart_dagster_repo()

                    repo_ready = True

                    with open(join(SHARED_DIR, f'{v.get("APP")}-syncs.json'), "w") as replace_file:
                        replace_file.write(resp.text)
                else:
                    repo_ready = True

            except Exception:
                logger.exception("Error while fetching sync jobs and creating dagster jobs")
            time.sleep(5)

    def insert_syncs_into_metastore(self, syncs: Json[any]):
        sync_service = get_syncs_service(next(get_session()))

        sync_schedules = {}
        for sync in syncs:
            obj = SyncScheduleCreate(
                sync_id=sync["id"],
                status=sync["status"],
                run_interval=sync["schedule"].get("run_interval", 60000),
            )
            sync_schedules[obj.sync_id] = obj

        sync_service.insert_or_update_list_of_schedules(sync_schedules)

    @exception_to_sys_exit
    @retry_on_exception
    def restart_dagster_repo(self) -> None:
        shutdown_info: ShutdownRepositoryLocationInfo = self.dagster_client.shutdown_repository_location(
            v.get("REPO_NAME")
        )
        if shutdown_info.status == ShutdownRepositoryLocationStatus.SUCCESS:
            logger.info("Dagster repo successfully shutdown")
        else:
            raise Exception(f"Repository location shutdown failed: {shutdown_info.message}")

    # make a zip archive of all dagster jobs
    def gen_dagster_job_archive(self, dirs: dict[str, str], syncs: Json[any]) -> None:
        print(syncs)
        for sync in syncs:
            self.gen_job_file(dirs, sync)
        # with open(join(dirs[GENERATED_DIR], f"__init__.py"), "w") as f:
        #    f.write("# Autogenerated: This file is needed to make a valid python package")
        shutil.make_archive(
            join(SHARED_DIR, f"{v.get('APP')}-valmi-jobs"), "zip", join(SHARED_DIR, v.get("APP"), "gen")
        )

    # use jinja2 to generate dagster a single job
    def gen_job_file(self, dirs: dict[str, str], sync: Json[any]) -> None:
        with open(join(dirs[GENERATED_CONFIG_DIR], f"{sync['id']}-{sync['source']['id']}.json"), "w") as f:
            f.write(json.dumps(sync["source"]["credential"]["connector_config"]))

        with open(join(dirs[GENERATED_CATALOG_DIR], f"{sync['id']}-{sync['source']['id']}.json"), "w") as f:
            f.write(json.dumps(sync["source"]["catalog"]))

        with open(join(dirs[GENERATED_CONFIG_DIR], f"{sync['id']}-{sync['destination']['id']}.json"), "w") as f:
            f.write(json.dumps(sync["destination"]["credential"]["connector_config"]))

        with open(join(dirs[GENERATED_CATALOG_DIR], f"{sync['id']}-{sync['destination']['id']}.json"), "w") as f:
            f.write(json.dumps(sync["destination"]["catalog"]))

        file_loader = FileSystemLoader(join(dirname(__file__), "templates"))
        env = Environment(loader=file_loader)
        template = env.get_template("job_template.jinja")

        output = template.render(sync=sync, app=v.get("APP"), prefix=SHARED_DIR)
        with open(join(dirs[GENERATED_DIR], f"{sync['id'].replace('-','_')}.py"), "w") as f:
            f.write(output)
