import os
import uuid
from pydantic import UUID4
import requests
from requests.adapters import HTTPAdapter, Retry

# TODO: Constants - need to come from current_run_details
HTTP_TIMEOUT = 3  # seconds
MAX_HTTP_RETRIES = 3
CONNECTOR_STRING = "dest"


# desanitise uuid
def du(uuid_str: str) -> UUID4:
    return uuid.UUID(uuid_str.replace("_", "-"))


class ConnectorState:
    def __init__(self, run_time_args=[]) -> None:
        self.num_chunks = run_time_args["chunk_id"]
        self.total_records = self.num_chunks * run_time_args["chunk_size"]
        self.run_time_args = run_time_args

    def register_chunk(self):
        self.num_chunks = self.num_chunks + 1

    def register_record(self):
        self.total_records = self.total_records + 1

    def register_records(self, num_records):
        self.total_records = self.total_records + num_records


class NullEngine:
    def __init__(self) -> None:
        self.connector_state = None
        pass

    def error(self):
        pass

    def success(self):
        pass

    def metric(self):
        pass

    def current_run_details(self):
        return {}

    def abort_required(self):
        return False


class Engine(NullEngine):
    def __init__(self, *args, **kwargs):
        super(Engine, self).__init__(*args, **kwargs)
        self.engine_url = os.environ["ACTIVATION_ENGINE_URL"]

        self.session_with_retries = requests.Session()
        # retry on all errors except 400 and 401
        status_forcelist = tuple(x for x in requests.status_codes._codes if x > 400 and x not in [400, 401])
        retries = Retry(total=MAX_HTTP_RETRIES, backoff_factor=5, status_forcelist=status_forcelist)
        self.session_with_retries.mount("http://", HTTPAdapter(max_retries=retries))

        self.session_without_retries = requests.Session()
        status_forcelist = []
        retries = Retry(total=0, backoff_factor=5, status_forcelist=status_forcelist)
        self.session_without_retries.mount("http://", HTTPAdapter(max_retries=retries))

        run_time_args = self.current_run_details()
        self.connector_state = ConnectorState(run_time_args=run_time_args)

    def current_run_details(self):
        sync_id = du(os.environ.get("DAGSTER_RUN_JOB_NAME", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"))
        r = self.session_with_retries.get(
            f"{self.engine_url}/syncs/{sync_id}/runs/current_run_details", timeout=HTTP_TIMEOUT
        )
        return r.json()

    def metric(self, commit=False):
        print("Sending metric")
        payload = {
            "sync_id": self.connector_state.run_time_args["sync_id"],
            "run_id": self.connector_state.run_time_args["run_id"],
            "chunk_id": self.connector_state.num_chunks,
            "connector_id": "dest",
            "metrics": {"success": self.connector_state.records_in_chunk},
        }

        print("payload ", payload)
        if commit:
            r = self.session_with_retries.post(
                f"{self.engine_url}/metrics/",
                timeout=HTTP_TIMEOUT,
                json=payload,
            )
            r.raise_for_status()
        else:
            r = self.session_without_retries.post(
                f"{self.engine_url}/metrics/",
                timeout=HTTP_TIMEOUT,
                json=payload,
            )

    def error(self, msg="error"):
        # TODO: finish this
        print("sending error ", msg)
        sync_id = self.connector_state.run_time_args["sync_id"]
        run_id = self.connector_state.run_time_args["run_id"]
        r = self.session_with_retries.post(
            f"{self.engine_url}/syncs/{sync_id}/runs/{run_id}/error/", timeout=HTTP_TIMEOUT, json={"error": msg}
        )
        r.raise_for_status()

    def abort_required(self):
        return False
        # TODO: finish this
        sync_id = self.connector_state.run_time_args["sync_id"]
        run_id = self.connector_state.run_time_args["run_id"]
        r = self.session_with_retries.get(f"{self.engine_url}/syncs/{sync_id}/runs/{run_id}", timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()["abort_required"]

    def checkpoint(self, state):
        print("sending checkpoint")
        sync_id = self.connector_state.run_time_args["sync_id"]
        run_id = self.connector_state.run_time_args["run_id"]
        r = self.session_with_retries.post(
            f"{self.engine_url}/syncs/{sync_id}/runs/{run_id}/state/{CONNECTOR_STRING}",
            timeout=HTTP_TIMEOUT,
            json=state,
        )
        r.raise_for_status()
