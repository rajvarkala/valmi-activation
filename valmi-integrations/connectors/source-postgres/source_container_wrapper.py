#!env python
import json
import os
import sys
from os.path import join
import subprocess
import io
import uuid
from pydantic import UUID4
import requests


MAGIC_NUM = 0x7FFFFFFF

HTTP_TIMEOUT = 3


# desanitise uuid
def du(uuid_str: str) -> UUID4:
    return uuid.UUID(uuid_str.replace("_", "-"))


class ConnectorState:
    def __init__(self, run_time_args=[]) -> None:
        self.num_chunks = run_time_args["chunk_id"]
        self.records_in_chunk = 0
        self.total_records = self.num_chunks * run_time_args["chunk_size"]
        self.run_time_args = run_time_args

    def register_chunk(self):
        self.num_chunks = self.num_chunks + 1
        self.records_in_chunk = 0

    def register_record(self):
        self.records_in_chunk = self.records_in_chunk + 1
        self.total_records = self.total_records + 1


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


class Engine(NullEngine):
    def __init__(self, *args, **kwargs):
        super(Engine, self).__init__(*args, **kwargs)
        run_time_args = self.current_run_details()
        self.connector_state = ConnectorState(run_time_args=run_time_args)

    def current_run_details(self):
        engine_url = os.environ["ACTIVATION_ENGINE_URL"]
        sync_id = du(os.environ.get("DAGSTER_RUN_JOB_NAME", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"))
        r = requests.get(f"{engine_url}/syncs/{sync_id}/runs/current_run_id", timeout=HTTP_TIMEOUT)
        return r.json()


class StdoutWriter:
    def __init__(self) -> None:
        pass

    def store(self, record):
        print(record)


class StoreWriter:
    def __init__(self, engine) -> None:
        self.engine = engine
        self.connector_state: ConnectorState = self.engine.connector_state
        store_config = json.loads(os.environ["VALMI_INTERMEDIATE_STORE"])
        if store_config["provider"] == "local":
            path_name = join(store_config["local"]["directory"], self.connector_state.run_time_args["run_id"])
            os.makedirs(path_name, exist_ok=True)

            self.path_name = path_name
            self.records = []
            self.counter = 0

    def write(self, record, last=False):
        self.records.append(record)
        self.connector_state.register_record()
        if self.connector_state.records_in_chunk >= self.connector_state.run_time_args["chunk_size"]:
            self.flush(last=False)
            self.records = []
            self.connector_state.register_chunk()
            self.engine.metric()
        elif self.connector_state.records_in_chunk % self.connector_state.run_time_args["records_per_metric"] == 0:
            self.engine.metric()

    def flush(self, last=False):
        list_dir = sorted([f.lower() for f in os.listdir(self.path_name)], key=lambda x: int(x[:-5]))
        new_file_name = f"{MAGIC_NUM}.vald" if last else (list_dir[-1] if len(list_dir) > 0 else "0.vald")
        with open(join(self.path_name, f"{int(new_file_name[:-5])+1}.vald"), "w") as f:
            for record in self.records:
                f.write(json.dumps(record))
                f.write("\n")

    def finalize(self):
        self.flush(last=True)


class DefaultHandler:
    def __init__(
        self, engine: Engine = None, store_writer: StoreWriter = None, stdout_writer: StdoutWriter = None
    ) -> None:
        self.engine = engine
        self.store_writer = store_writer
        self.stdout_writer = stdout_writer
        pass

    def handle(self, record):
        print(json.dumps(record))

    def finalize(self):
        pass


class LogHandler(DefaultHandler):
    def __init__(self, *args, **kwargs):
        super(LogHandler, self).__init__(*args, **kwargs)

    def handle(self, record):
        print(json.dumps(record))


class CheckpointHandler(DefaultHandler):
    def __init__(self, *args, **kwargs):
        super(CheckpointHandler, self).__init__(*args, **kwargs)

    def handle(self, record):
        print(json.dumps(record))


class RecordHandler(DefaultHandler):
    def __init__(self, *args, **kwargs):
        super(RecordHandler, self).__init__(*args, **kwargs)

    def handle(self, record):
        self.store_writer.write(record)

    def finalize(self):
        self.store_writer.finalize()


handlers = {
    "LOG": LogHandler,
    "CHECKPOINT": CheckpointHandler,
    "RECORD": RecordHandler,
    "default": DefaultHandler,
}


def get_airbyte_command():
    entrypoint_str = os.environ["VALMI_ENTRYPOINT"]
    entrypoint = entrypoint_str.split(" ")

    airbyte_command = sys.argv[3]
    for i, arg in enumerate(sys.argv[1:]):
        if i >= len(entrypoint):
            airbyte_command = arg
            break

    return airbyte_command


def get_config_file_path():
    # TODO: do this better
    config_file_path = None
    for i, arg in enumerate(sys.argv):
        if arg == "--config":
            config_file_path = sys.argv[i + 1]
            break

    return config_file_path


def populate_run_time_args(airbyte_command, engine, config_file_path):
    if airbyte_command == "read":
        run_time_args = engine.current_run_details()

        with open(config_file_path, "r") as f:
            config = json.loads(f.read())

        with open(config_file_path, "w") as f:
            config["run_time_args"] = run_time_args
            f.write(json.dumps(config))


def main():
    airbyte_command = get_airbyte_command()
    config_file = get_config_file_path()

    if airbyte_command is None or config_file is None:
        sys.exit(5)

    # if arg in read, write:
    # read checkpoint from the engine

    if airbyte_command == "read":
        engine = Engine()
    else:
        engine = NullEngine()

    # populate run_time_args
    populate_run_time_args(airbyte_command, engine)

    # store writer
    store_writer = StoreWriter(engine)
    stdout_writer = StdoutWriter()

    # initialize handlers
    for key in handlers.keys():
        handlers[key] = handlers[key](engine=engine, store_writer=store_writer, stdout_writer=stdout_writer)

    # create the subprocess
    proc = subprocess.Popen(
        sys.argv[1:],
        stdout=subprocess.PIPE,
    )

    record_types = handlers.keys()
    for line in io.TextIOWrapper(proc.stdout, encoding="utf-8"):  # or another encoding
        if line.strip() == "":
            continue
        json_record = json.loads(line)
        if json_record["type"] not in record_types:
            handlers["default"].handle(json_record)
        else:
            handlers[json_record["type"]].handle(json_record)

    return_code = proc.poll()
    if return_code != 0:
        engine.error()
        sys.exit(return_code)
    else:
        if airbyte_command not in ["spec", "check", "discover"]:
            store_writer.finalize()
        engine.success()


if __name__ == "__main__":
    main()
