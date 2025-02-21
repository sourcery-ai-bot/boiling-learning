from __future__ import annotations

import datetime
import shlex
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Hashable,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    TypeVar,
    Union,
)

import json_tricks
import more_itertools as mit
import requests
import zict
from pkg_resources import resource_filename

import boiling_learning as bl
from boiling_learning.utils.utils import (
    JSONDict,
    PathLike,
    empty_gen,
    ensure_dir,
    ensure_resolved,
    fix_path,
    indexify,
    print_verbose,
    rmdir,
)

_T = TypeVar('_T')


def apply_to_obj(tpl: Tuple[Any, str, Iterable, Mapping[str, Any]]):
    if len(tpl) != 4:
        raise ValueError(
            'expected a tuple in the format (obj, fname, args, kwargs)'
        )

    obj, fname, args, kwargs = tpl
    return getattr(obj, fname)(*args, **kwargs)


def apply_to_f(tpl: Tuple[Callable, Iterable, Mapping[str, Any]]):
    if len(tpl) != 3:
        raise ValueError('expected a tuple in the format (f, args, kwargs)')

    f, args, kwargs = tpl
    return f(*args, **kwargs)


def distribute_iterable(
    keys: Sequence[Hashable],
    iterable: Iterable,
    assignments: Optional[Mapping[Hashable, Iterable]] = None,
    assign_pred: Optional[Callable[[Hashable], bool]] = None,
    assign_iterable: Optional[Iterable] = None,
) -> Dict[Hashable, List]:
    if (assign_pred, assign_iterable).count(None) == 1:
        raise ValueError(
            'either both or none of assign_pred and assign_iterable must be passed as arguments.'
        )

    if assign_iterable is not None:
        assignments = distribute_iterable(
            [key for key in keys if assign_pred(key)],
            assign_iterable,
            assignments,
        )

    if assignments is None:
        n_keys = len(keys)

        return dict(zip(keys, map(list, mit.distribute(n_keys, iterable))))
    else:
        distributed = bl.utils.merge_dicts(
            {k: [] for k in keys}, {k: list(v) for k, v in assignments.items()}
        )
        distributed_items = sorted(
            distributed.items(), key=(lambda pair: len(pair[1]))
        )

        n_keys = len(distributed_items)
        level, pos = 0, 0
        for item in iterable:
            distributed_items[pos][1].append(item)
            pos += 1
            if pos == n_keys or len(distributed_items[pos][1]) > level:
                level += 1
                pos = 0

        return dict(distributed_items)


class BaseUserPool:
    def __init__(self, enabled: bool):
        self.is_enabled = enabled

    def enable(self) -> None:
        self.is_enabled = True

    def disable(self) -> None:
        self.is_enabled = False

    @contextmanager
    def enabled(self) -> Iterator:
        prev_state = self.is_enabled
        self.enable()
        yield self
        self.is_enabled = prev_state

    @contextmanager
    def disabled(self) -> Iterator:
        prev_state = self.is_enabled
        self.disable()
        yield self
        self.is_enabled = prev_state


class UserPool(BaseUserPool):
    # See <https://stackoverflow.com/a/23665658/5811400>

    def __init__(
        self,
        workers: Iterable[Hashable],
        manager: Optional[Hashable] = None,
        current: Optional[Hashable] = None,
        server: Optional[Hashable] = None,
        workers_key: str = 'allowed_users',
        manager_key: str = 'manager',
        server_key: str = 'server',
        enabled: bool = True,
    ):
        super().__init__(enabled)

        if manager is None:
            workers = mit.peekable(workers)
            manager = workers[0]
        self.manager = manager

        self.workers = sorted(workers)

        if current is not None:
            self.current = current
        self.server = server

        self.workers_key = workers_key
        self.manager_key = manager_key
        self.server_key = server_key
        self.is_enabled = enabled

    def __getitem__(self, key: int) -> Hashable:
        return self.workers[key]

    def __len__(self) -> int:
        return len(self.workers)

    @property
    def current(self):
        return self._current

    @current.setter
    def current(self, current: Hashable) -> None:
        if current not in self:
            raise ValueError(
                f'notebook user {current} is not expected. Allowed users are {self.workers}.'
            )
        self._current = current

    @property
    def clients(self) -> List[Hashable]:
        return [worker for worker in self if worker != self.manager]

    @classmethod
    def from_json(
        cls,
        path: PathLike,
        workers_key: str = 'allowed_users',
        manager_key: str = 'manager',
        server_key: str = 'server',
    ) -> UserPool:
        config = bl.io.load_json(path)

        return cls(
            workers=config[workers_key],
            manager=config.get(manager_key),
            server=config.get(server_key),
        )

    def to_json(self, path: PathLike) -> None:
        obj = {
            self.workers_key: self.workers,
            self.manager_key: self.manager,
        }
        if self.server is not None:
            obj[self.server_key] = self.server
        bl.io.save_json(obj, path)

    def distribute_iterable(
        self,
        iterable: Iterable,
        assignments: Optional[Mapping[Hashable, Iterable]] = None,
        assign_pred: Optional[Callable[[Hashable], bool]] = None,
        assign_iterable: Optional[Iterable] = None,
    ) -> Mapping[Hashable, Iterable]:
        if assignments is not None:
            user_diff = set(assignments.keys()) - set(self)
            if user_diff:
                raise ValueError(f'some users were not expected: {user_diff}')

        return distribute_iterable(
            self,
            iterable,
            assignments=assignments,
            assign_pred=assign_pred,
            assign_iterable=assign_iterable,
        )

    def get_iterable(self, iterable: Iterable) -> Iterable:
        if self.is_enabled:
            return self.distribute_iterable(iterable)[self.current]
        else:
            return iterable

    def is_manager(self) -> bool:
        return self.current == self.manager

    def is_client(self) -> bool:
        return self.current in self.clients

    def is_server(self) -> bool:
        return self.current == self.server


class DynamicUserPool(BaseUserPool):
    def __init__(
        self,
        path: PathLike,
        workers: Optional[Iterable] = None,
        overwrite: bool = False,
        reset: bool = False,
        enabled: bool = True,
    ):
        super().__init__(enabled)

        if reset:
            rmdir(path, keep=True, recursive=True, missing_ok=True)

        self.path = ensure_dir(path)
        self._cases_path = self.path / 'cases'
        self._users_path = self.path / 'users'

        self._data = JSONDict(
            self._users_path, dumps=json_tricks.dumps, loads=json_tricks.loads
        )
        self._cases = JSONDict(
            self._cases_path, dumps=json_tricks.dumps, loads=json_tricks.loads
        )
        self._current_ticket: Optional[int] = None

        if workers is not None:
            if overwrite or 'available_tickets' not in self._data:
                self._data['available_tickets'] = list(indexify(workers))
            if overwrite or 'used_tickets' not in self._data:
                self._data['used_tickets'] = set()

    def __str__(self) -> str:
        return ''.join(
            (
                self.__class__.__name__,
                '(',
                ', '.join(
                    (
                        f'available_tickets={self.available_tickets()}',
                        f'current_ticket={self.current_ticket()}',
                        f'used_tickets={self.used_tickets()}',
                    )
                ),
                ')',
            )
        )

    def __getitem__(self, key: int) -> Hashable:
        return self.available_tickets()[key]

    def __len__(self) -> int:
        return len(self.available_tickets())

    def current_ticket(self) -> Optional[int]:
        return self._current_ticket

    def available_tickets(self) -> List[int]:
        return self._data['available_tickets']

    def used_tickets(self) -> Set[int]:
        return self._data['used_tickets']

    def is_stamped(self) -> bool:
        return self.current_ticket is not None

    def stamp_ticket(self) -> int:
        available_tickets = self._data['available_tickets']
        used_tickets = self._data['used_tickets']

        ticket = available_tickets.pop(0)
        self._current_ticket = ticket
        used_tickets.add(ticket)

        self._data['available_tickets'] = available_tickets
        self._data['used_tickets'] = used_tickets
        return ticket

    def _open_case(self, case_name: str) -> zict.Func:
        if case_name not in self._cases:
            self._cases[case_name] = {
                'available_tickets': self._data['available_tickets'],
                'used_tickets': self._data['used_tickets'],
            }

    def distribute_iterable(
        self,
        case_name: str,
        iterable: Iterable,
        assignments: Optional[Mapping[Hashable, Iterable]] = None,
        assign_pred: Optional[Callable[[Hashable], bool]] = None,
        assign_iterable: Optional[Iterable] = None,
    ) -> Mapping[Hashable, Iterable]:
        self._open_case(case_name)

        if assignments is not None:
            user_diff = set(assignments.keys()) - set(self)
            if user_diff:
                raise ValueError(f'some users were not expected: {user_diff}')

        return distribute_iterable(
            self._cases[case_name]['used_tickets'],
            iterable,
            assignments=assignments,
            assign_pred=assign_pred,
            assign_iterable=assign_iterable,
        )

    def get_iterable(self, case_name: str, iterable: Iterable) -> Iterable:
        case_dict = JSONDict(
            self._cases_path, dumps=json_tricks.dumps, loads=json_tricks.loads
        )
        if case_name not in case_dict:
            case_dict[case_name] = self._data

        if not self.is_enabled:
            return iterable

        current_ticket = self.current_ticket()

        return (
            self.distribute_iterable(case_name, iterable)[current_ticket]
            if current_ticket in self._cases[case_name]['used_tickets']
            else empty_gen()
        )


class LFSSequenceDistributor:
    def __init__(self, path: PathLike, reset: bool = False):
        path = ensure_dir(path)
        if reset:
            rmdir(path, recursive=True, missing_ok=True, keep=True)
        self.path = path
        self._cases = JSONDict(
            self.path, dumps=json_tricks.dumps, loads=json_tricks.loads
        )

    def current(self, case_name: str) -> int:
        return self._cases[case_name]['current']

    def _set_case(
        self, case_name: str, total_length: int, current: int
    ) -> None:
        self._cases[case_name] = {
            'name': case_name,
            'total_length': total_length,
            'current': current,
        }

    def get(self, case_name: str, seq: Sequence[_T]) -> Iterator[_T]:
        total_length = len(seq)

        if case_name not in self._cases:
            self._set_case(case_name, total_length, 0)

        while (
            self.current(case_name) is None
            or self.current(case_name) < total_length
        ):
            current = self.current(case_name)
            self._set_case(case_name, total_length, current + 1)
            yield seq[current]


class SequenceDistributorServer:
    def __init__(
        self,
        data_dir: PathLike,
        port: int = 8000,
        venv_name: Optional[str] = None,
    ):
        self.data_dir = ensure_dir(data_dir)

        self._port = port
        self._venv_name = venv_name

        self._logs_filepath = self.data_dir / 'logs.txt'
        self._public_url_filepath = self.data_dir / 'url.txt'
        if self._public_url_filepath.is_file():
            self._public_url_filepath.unlink()

        self._main_filepath = fix_path(
            resource_filename(__name__, '_worker_resources/main.py')
        )
        self._requirements_filepath = fix_path(
            resource_filename(__name__, '_worker_resources/requirements.txt')
        )

    @property
    def public_url(self) -> Optional[str]:
        if self._public_url_filepath.is_file():
            return self._public_url_filepath.read_text()
        else:
            return None

    def display_log(self) -> None:
        if self._logs_filepath.is_file():
            with open(self._logs_filepath) as logs:
                print(''.join(logs))

    def _log(self, text: str, end: str = '\n') -> None:
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with self._logs_filepath.open('a') as log:
            log.write(f'[{now}] {text}{end}')

    def run(self) -> None:
        if self._venv_name is None:
            venv_command = ()
        else:
            venv_activate_path = Path(
                self._venv_name, 'bin', 'activate'
            ).resolve()

            if not venv_activate_path.is_file():
                make_venv_command = (f'python -m venv {self._venv_name}',)
            else:
                make_venv_command = ()

            venv_command = make_venv_command + (
                f'source {self._venv_name}/bin/activate',
            )

        run_python_commands = venv_command + (
            f'pip install -r "{self._requirements_filepath}"',
            f'python {self._main_filepath} {self._port} "{self._public_url_filepath}"',
        )

        for command in run_python_commands:
            self._log(f'Running: {command}')
            subprocess.run(shlex.split(command))

        # self.tunnel = ngrok.connect(str(self._port))
        self._log('Connected.')
        self._log(f'public url: {self.public_url}')


class SequenceDistributorClient:
    def __init__(self, url: str):
        self.url: str = url

    @classmethod
    def from_file(
        path: PathLike, sleep_time: int = 0, verbose: bool = False
    ) -> SequenceDistributorClient:
        url_path = ensure_resolved(path)

        if sleep_time > 0:
            while not url_path.is_file():
                print_verbose(verbose, 'File not found:', url_path)
                print_verbose(verbose, f'Sleeping for {sleep_time}s')
                time.sleep(sleep_time)

        url = url_path.read_text()

        return SequenceDistributorClient(url)

    def connect(self) -> bool:
        url = self.url + '/'
        try:
            r = requests.get(url)
            r.raise_for_status()
            return True
        except requests.HTTPError:
            return False

    def assign(
        self, case_name: str, seq: Union[int, Sequence]
    ) -> Optional[int]:
        if not isinstance(seq, int):
            seq = len(seq)

        url = self.url + '/assign'
        r = requests.get(url, params={'case_name': case_name, 'seq': seq})
        r.raise_for_status()
        try:
            return r.json()
        except KeyError:
            raise RuntimeError('response does not contain a *index* field')

    def complete(self, case_name: str, index: int) -> None:
        url = self.url + '/complete'
        requests.put(url, data={'case_name': case_name, 'index': index})

    def consume(self, case_name: str, seq: Sequence[_T]) -> Iterator[_T]:
        index = self.assign(case_name, seq)
        while index is not None:
            yield seq[index]
            self.complete(case_name, index)
            index = self.assign(case_name, seq)
