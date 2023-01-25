# Copyright 2023 Efabless Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import annotations

import os
import datetime
import subprocess
from abc import abstractmethod, ABC
from concurrent.futures import Future, ThreadPoolExecutor
from typing import List, Tuple, Type, ClassVar, Optional, Dict, final

from rich.progress import (
    Progress,
    TextColumn,
    BarColumn,
    MofNCompleteColumn,
    TimeElapsedColumn,
    TaskID,
)

from ..config import Config
from ..steps import (
    MissingInputError,
    Step,
    TclStep,
    State,
)
from ..utils import Toolbox
from ..common import mkdirp, console, log, err, success


class Flow(ABC):
    """
    An abstract base class for a flow.

    Flows encapsulates a subroutine that runs multiple steps: either synchronously,
    asynchronously, serially or in any manner.

    The Flow ABC offers a number of convenience functions, including handling the
    progress bar at the bottom of the terminal, which shows what stage the flow
    is currently in and the remaining stages.

    Properties:

        :param name: An optional string name for the flow
        :param Steps: A list of Step **types** used by the Flow (not Step objects.)
    """

    name: Optional[str] = None
    Steps: ClassVar[List[Type[Step]]] = [
        TclStep,
    ]

    def __init__(self, config: Config, design_dir: str):
        """
        :param config: The configuration object used for this flow.
        :param design_dir: The design directory of the flow, i.e., the `dirname`
            of the `config.json` file from which it was generated.
        """
        self.config: Config = config
        self.steps: List[Step] = []
        self.design_dir = design_dir

        self.tpe: ThreadPoolExecutor = ThreadPoolExecutor()

        self.ordinal: int = 0
        self.max_stage: int = 0
        self.task_id: Optional[TaskID] = None
        self.progress: Optional[Progress] = None
        self.run_dir: Optional[str] = None
        self.tmp_dir: Optional[str] = None
        self.toolbox: Optional[Toolbox] = None

    def get_name(self) -> str:
        """
        :returns: The name of the Flow. If `self.name` is None, the class's name
            is returned.
        """
        return self.name or self.__class__.__name__

    def set_max_stage_count(self, count: int):
        """
        A helper function, used to set the total number of stages a flow is
        expected to go through. Used to set the progress bar.

        :param count: The total number of stages.
        """
        if self.progress is None or self.task_id is None:
            return
        self.max_stage = count
        self.progress.update(self.task_id, total=count)

    def start_stage(self, name: str):
        """
        Starts a new stage, updating the progress bar appropriately.

        :param name: The name of the stage.
        """
        if self.progress is None or self.task_id is None:
            return
        self.ordinal += 1
        self.progress.update(
            self.task_id,
            description=f"{self.get_name()} - Stage {self.ordinal} - {name}",
        )

    def end_stage(self):
        """
        Ends the current stage, updating the progress bar appropriately.
        """
        self.progress.update(self.task_id, completed=float(self.ordinal))

    def current_stage_prefix(self) -> str:
        """
        Returns a prefix for a step ID with its stage number so it can be used
        to create a step directory.
        """
        max_stage_digits = len(str(self.max_stage))
        return f"%0{max_stage_digits}d-" % self.ordinal

    def dir_for_step(self, step: Step):
        """
        Returns a directory within the run directory for a specific step,
        prefixed with the current progress bar stage number.
        """
        if self.run_dir is None:
            raise Exception("")
        return os.path.join(
            self.run_dir,
            f"{self.current_stage_prefix()}{step.id}",
        )

    @final
    def start(
        self,
        with_initial_state: Optional[State] = None,
        tag: Optional[str] = None,
    ) -> Tuple[bool, List[State]]:
        """
        The entry point for a flow.

        :param with_initial_state: An optional initial state object to use.
            If not provided, a default empty state is created.
        :param tag: A name for this invocation of the flow. If not provided,
            one based on a date string will be created.

        :returns: `(success, state_list)`
        """
        if tag is None:
            tag = datetime.datetime.now().astimezone().strftime("RUN_%Y-%m-%d_%H-%M-%S")

        self.run_dir = os.path.join(self.design_dir, "runs", tag)
        self.tmp_dir = os.path.join(self.run_dir, "tmp")
        self.toolbox = Toolbox(self.tmp_dir)

        mkdirp(self.run_dir)

        config_res_path = os.path.join(self.run_dir, "resolved.json")
        with open(config_res_path, "w") as f:
            f.write(self.config.dumps())

        self.progress = Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
        )
        self.progress.start()
        self.task_id = self.progress.add_task(
            f"{self.get_name()}",
        )
        result = self.run(
            with_initial_state=with_initial_state,
        )
        self.progress.stop()

        # Reset stateful objects
        self.progress = None
        self.task_id = None
        self.tmp_dir = None
        self.toolbox = None
        self.ordinal = 0
        self.max_stage = 0

        return result

    @abstractmethod
    def run(
        self,
        with_initial_state: Optional[State] = None,
    ) -> Tuple[bool, List[State]]:
        """
        The core of the Flow. Subclasses of flow are expected to override this
        method.

        This method is considered private and should only be called by Flow or
        its subclasses.

        :param with_initial_state: An optional initial state object to use.
            If not provided, a default empty state is created.
        :returns: `(success, state_list)`
        """
        pass

    def run_step_async(self, step: Step, *args, **kwargs) -> Future[State]:
        """
        A helper function that may run a step asynchronously.

        It returns a `Future` encapsulating a State object, which can then be
        used as an input to the next step or inspected to await it.

        See the Step initializer for more info.

        :param step: The step object to run
        :param args: Arguments to `step.start`
        :param kwargs: Keyword arguments to `step.start`
        """
        return self.tpe.submit(step.start, *args, **kwargs)


class SequentialFlow(Flow):
    """
    The simplest Flow, running each Step as a stage, serially,
    with nothing happening in parallel and no significant inter-step
    processing.

    All subclasses of this flow have to do is override the `Steps` property
    and it would automatically handle the rest. See `Basic` for an example.
    """

    def run(
        self,
        with_initial_state: Optional[State] = None,
    ) -> Tuple[bool, List[State]]:
        step_count = len(self.Steps)
        self.set_max_stage_count(step_count)

        initial_state = with_initial_state or State()
        state_list = [initial_state]
        log("Starting…")
        for cls in self.Steps:
            step = cls()
            self.steps.append(step)
            self.start_stage(step.name)
            try:
                new_state = step.start()
            except MissingInputError as e:
                err(f"Misconfigured flow: {e}")
                return (False, state_list)
            except subprocess.CalledProcessError:
                err("An error has been encountered. The flow will stop.")
                return (False, state_list)
            state_list.append(new_state)
            self.end_stage()
        success("Flow complete.")
        return (True, state_list)


class FlowFactory(object):
    """
    A factory singleton for Flows, allowing Flow types to be registered and then
    retrieved by name.

    See https://en.wikipedia.org/wiki/Factory_(object-oriented_programming) for
    a primer.
    """

    _registry: ClassVar[Dict[str, Type[Flow]]] = {}

    @classmethod
    def register(Self, flow: Type[Flow]):
        """
        Adds a flow type to the registry with its Python name as a lookup string.

        :param flow: A Flow **type** (not object)
        """
        name = flow.__name__
        Self._registry[name] = flow

    @classmethod
    def get(Self, name: str) -> Optional[Type[Flow]]:
        """
        Retrieves a Flow type from the registry using its Python name as a lookup
        string.

        :param name: The Python name of the Flow. Case-sensitive.
        """
        return Self._registry.get(name)

    @classmethod
    def list(Self) -> List[str]:
        """
        :returns: A list of strings representing Python names of all registered
        flows.
        """
        return list(Self._registry.keys())