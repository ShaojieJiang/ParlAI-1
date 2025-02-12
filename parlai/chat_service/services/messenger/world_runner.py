#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
"""World Runner Module.

The World Runner provides the manager with utility functions for running
overworlds, onboard worlds, and task worlds.
"""
import shared_utils as utils
import time
import datetime
from concurrent import futures
import logging


class MessengerWorldRunner:
    """World Runner.

    Launches worlds, overworlds, etc. Helper for MessengerManager
    """

    def __init__(self, opt, world_path, max_workers, manager, is_debug=False):
        self._world_module = utils.get_world_module(world_path)
        self.executor = futures.ThreadPoolExecutor(max_workers=max_workers)
        self.debug = is_debug
        self._log("Found world module: {}".format(self._world_module))
        opt["is_debug"] = is_debug
        self.manager = manager
        self.system_done = False
        self.opt = opt
        self.tasks = {}  # task ID to task

    def _log(self, text):
        if self.debug:
            time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print("{} DEBUG: {}".format(time, text))

    def shutdown(self):
        """Shutdown the world runner."""
        for _, task in self.tasks.items():
            if task.world is not None:
                task.world.shutdown()

        self.system_done = True  # this forces worlds to stop executing parley
        self._log("Executor shutting down.")
        self.executor.shutdown()
        self._log("Shutdown complete.")

    def _run_world(self, task, world_name, agents):
        """Run a world until completion.

        :param task:
            TaskState. State of the given task.
        :param world_name:
            string. The name of the world in the module file
        :param agents:
            list. A list of agents that should be in the world.

        :return:
            ret_val: last output of world's parley function. Return None if ERROR
            world_data: data attribute of world, if it has one
        """
        ret_val = None
        world_generator = utils.get_world_fn_attr(
            self._world_module, world_name, "generate_world"
        )
        world = world_generator(self.opt, agents)
        task.world = world

        while not world.episode_done() and not self.system_done:
            ret_val = world.parley()
            time.sleep(0.3)
        world.shutdown()
        world_data = world.data if hasattr(world, "data") else {}
        return ret_val, world_data

    def launch_task_world(self, task_name, world_name, agents):
        """Launch a task world.

        Return the job's future.

        :param task_name:
            string. the name of the job thread
        :param world_name:
            string. the name of the task world in the module file
        :param agents:
            list. the list of agents to install in the world

        :return:
            the Futures object corresponding to this launched task
        """
        task = utils.TaskState(task_name, world_name, agents)
        self.tasks[task_name] = task

        def _world_fn():
            utils.print_and_log(logging.INFO, 'Starting task {}...'.format(task_name))
            return self._run_world(task, world_name, agents)

        fut = self.executor.submit(_world_fn)
        task.future = fut
        return fut

    def launch_overworld(self, task_name, overworld_name, onboard_map, overworld_agent):
        """Launch an overworld and a subsequent onboarding world.

        Return the job's future

        :param task_name:
            string. the name of the job thread
        :param overworld_name:
            string. the name of the overworld in the module file
        :param onboard_map:
            map. a mapping of overworld return values to the names
            of onboarding worlds in the module file.
        :param overworld_agent:
            The agent to run the overworld with

        :return:
            the Futures object corresponding to running the overworld
        """
        task = utils.TaskState(
            task_name,
            overworld_name,
            [overworld_agent],
            is_overworld=True,
            world_type=None,
        )
        self.tasks[task_name] = task
        agent_state = self.manager.get_agent_state(overworld_agent.id)

        def _world_function():
            world_generator = utils.get_world_fn_attr(
                self._world_module, overworld_name, "generate_world"
            )
            overworld = world_generator(self.opt, [overworld_agent])
            while not self.system_done:
                world_type = overworld.parley()
                if world_type is None:
                    time.sleep(0.5)
                    continue

                # perform onboarding
                onboard_type = onboard_map.get(world_type)
                if onboard_type:
                    onboard_id = 'onboard-{}-{}'.format(overworld_agent.id, time.time())
                    agent = self.manager._create_agent(onboard_id, overworld_agent.id)
                    agent_state.set_active_agent(agent)
                    agent_state.assign_agent_to_task(agent, onboard_id)
                    _, onboard_data = self._run_world(task, onboard_type, [agent])
                    agent_state.onboard_data = onboard_data
                self.manager.add_agent_to_pool(agent_state, world_type)
                utils.print_and_log(logging.INFO, 'onboarding/overworld complete')
                time.sleep(5)

                # sleep until agent returns from task world
                while agent_state.get_active_agent() != overworld_agent:
                    time.sleep(2)
                overworld.return_overworld()
            return world_type

        fut = self.executor.submit(_world_function)
        task.future = fut
        return fut
