############################################################
#
#    Copyright (c) 2010, Southpaw Technology
#                        All Rights Reserved
#
#    PROPRIETARY INFORMATION.  This software is proprietary to
#    Southpaw Technology, and is not to be reproduced, transmitted,
#    or disclosed in any way without written permission.
#
#
import tacticenv

from pyasm.security import Batch
from pyasm.common import Common, Config, Environment, jsonloads, jsondumps, TacticException
from pyasm.biz import Project
from pyasm.search import Search, DbContainer
from pyasm.command import Command
from tactic.command import Scheduler, SchedulerTask

import os

__all__ = ['JobTask']

# create a task from the job
class JobTask(SchedulerTask):

    def __init__(my):
        #print "JobTask: init"
        my.job = None
        my.jobs = []


        my.check_interval = 1
        my.max_jobs = 2


        super(JobTask, my).__init__()


    def set_check_interval(my, interval):
        my.check_interval = interval

    def get_process_key(my):
        import platform;
        host = platform.uname()[1]
        pid = os.getpid()
        return "%s:%s" % (host, pid)


    def get_job_search_type(my):
        return "sthpw/queue"


    def get_next_job(my):
        from pyasm.prod.queue import Queue
        return Queue.get_next_job();




    def cleanup_db_jobs(my):
        # clean up the jobs that this host previously had

        process_key = my.get_process_key()
        job_search = Search(my.get_job_search_type())
        job_search.add_filter("host", process_key)
        my.jobs = job_search.get_sobjects()
        my.cleanup()



    def cleanup(my, count=0):
        #print "Cleaning up ..."
        if count >= 3:
            return
        try:
            for job in my.jobs:
                # reset all none complete jobs to pending

                current_state = job.get_value("state")
                if current_state not in ['locked']:
                    continue

                #print "setting to pending"
                job.set_value("state", "pending")
                job.set_value("host", "")
                job.commit()

            my.jobs = []

        except Exception, e:
            print "Exception: ", e.message
            count += 1
            my.cleanup(count)
            


    def execute(my):

        import atexit
        import time
        atexit.register( my.cleanup )

        while 1:
            my.check_existing_jobs()
            my.check_new_job()
            time.sleep(my.check_interval)
            #DbContainer.close_thread_sql()


    def check_existing_jobs(my):
        my.keep_jobs = []
        for job in my.jobs:
            job_code = job.get_code()
            search = Search(my.get_job_search_type())
            search.add_filter("code", job_code)
            job = search.get_sobject()

            if not job:
                print "Cancel ...."
                scheduler = Scheduler.get()
                scheduler.cancel_task(job_code)
                continue

            state = job.get_value("state")
            if state == 'cancel':
                print "Cancel task [%s] ...." % job_code
                scheduler = Scheduler.get()
                scheduler.cancel_task(job_code)

                job.set_value("state", "terminated")
                job.commit()
                continue

            my.keep_jobs.append(job)

        my.jobs = my.keep_jobs



    def check_new_job(my):

        num_jobs = len(my.jobs)
        if num_jobs >= my.max_jobs:
            print "Already at max jobs [%s]" % my.max_jobs
            return


        my.job = my.get_next_job()
        if not my.job:
            return

        # set the process key
        process_key = my.get_process_key()
        my.job.set_value("host", process_key)
        my.job.commit()

        my.jobs.append(my.job)

        # get some info from the job
        command = my.job.get_value("command")
        job_code = my.job.get_value("code")
        #print "Grabbing job [%s] ... " % job_code

        try: 
            kwargs = my.job.get_json_value("data")
        except:
            try:
                kwargs = my.job.get_json_value("serialized")
            except:
                kwargs = {}

        project_code = my.job.get_value("project_code")
        login = my.job.get_value("login")
        script_path = my.job.get_value("script_path", no_exception=True)


        if script_path:
            Project.set_project(project_code)
            command = 'tactic.command.PythonCmd'

            folder = os.path.dirname(script_path)
            title = os.path.basename(script_path)

            search = Search("config/custom_script")
            search.add_filter("folder", folder)
            search.add_filter("title", title)
            custom_script = search.get_sobject()
            script_code = custom_script.get_value("script")

            kwargs['code'] = script_code



        # add the job to the kwargs
        kwargs['job'] = my.job

        #print "command: ", command
        #print "kwargs: ", kwargs


        # Because we started a new thread, the environment may not
        # yet be initialized
        try:
            from pyasm.common import Environment
            Environment.get_env_object()
        except:
            print "running batch"
            Batch()


        queue = my.job.get_value("queue", no_exception=True)
        queue_type = 'repeat'

        print "running job: ", my.job.get_value("code") 

        if queue_type == 'inline':

            cmd = Common.create_from_class_path(command, kwargs=kwargs)
            try:
                Command.execute_cmd(cmd)

                # set job to complete
                my.job.set_value("state", "complete")
            except Exception, e:
                my.job.set_value("state", "error")

            my.job.commit()
            my.jobs.remove(my.job)
            my.job = None


        elif queue_type == 'repeat':

            cmd = Common.create_from_class_path(command, kwargs=kwargs)
            attempts = 0
            max_attempts = 5
            retry_interval = 10
            while 1:
                try:
                    #Command.execute_cmd(cmd)
                    cmd.execute()

                    # set job to complete
                    my.job.set_value("state", "complete")
                    break
                except TacticException, e:

                    # This is an error on this server, so just exit
                    # and don't bother retrying
                    print "Error: ", e
                    my.job.set_value("state", "error")
                    break


                except Exception, e:
                    raise
                    print "WARNING in Queue: ", e
                    import time
                    time.sleep(retry_interval)
                    attempts += 1
                    print "Retrying [%s]...." % attempts

                    if attempts >= max_attempts:
                        print "ERROR: reached max attempts"
                        my.job.set_value("state", "error")
                        break


            my.job.commit()
            my.jobs.remove(my.job)
            my.job = None




        else:
            class ForkedTask(SchedulerTask):
                def __init__(my, **kwargs):
                    super(ForkedTask, my).__init__(**kwargs)
                def execute(my):
                    # check to see the status of this job
                    """
                    job = my.kwargs.get('job')
                    job_code = job.get_code()
                    search = Search("sthpw/queue")
                    search.add_filter("code", job_code)
                    my.kwargs['job'] = search.get_sobject()

                    if not job:
                        print "Cancelling ..."
                        return

                    state = job.get_value("state")
                    if state == "cancel":
                        print "Cancelling 2 ...."
                        return
                    """

                    subprocess_kwargs = {
                        'login': login,
                        'project_code': project_code,
                        'command': command,
                        'kwargs': kwargs
                    }
                    subprocess_kwargs_str = jsondumps(subprocess_kwargs)
                    install_dir = Environment.get_install_dir()
                    python = Config.get_value("services", "python")
                    if not python:
                        python = 'python'
                    args = ['%s' % python, '%s/src/tactic/command/queue.py' % install_dir]
                    args.append(subprocess_kwargs_str)

                    import subprocess
                    p = subprocess.Popen(args)

                    DbContainer.close_thread_sql()

                    return

                    # can't use a forked task ... need to use a system call
                    #Command.execute_cmd(cmd)

            # register this as a forked task
            task = ForkedTask(name=job_code, job=my.job)
            scheduler = Scheduler.get()
            scheduler.start_thread()

            # FIXME: the queue should not be inline
            if queue == 'interval':

                interval = my.job.get_value("interval")
                if not interval:
                    interval = 60

                scheduler.add_interval_task(task, interval=interval,mode='threaded')

            else:
                scheduler.add_single_task(task, mode='threaded')



    def start():

        scheduler = Scheduler.get()
        scheduler.start_thread()

        task = JobTask()
        task.cleanup_db_jobs()

        scheduler.add_single_task(task, mode='threaded', delay=1)
    start = staticmethod(start)



def run_batch(kwargs):
    command = k.get("command")
    kwargs = k.get("kwargs")
    login = k.get("login")
    project_code = k.get("project_code")

    from pyasm.security import Batch
    Batch(project_code=project_code, login_code=login)

    cmd = Common.create_from_class_path(command, kwargs=kwargs)
    Command.execute_cmd(cmd)



__all__.append("QueueTest")

class QueueTest(Command):
    def execute(my):
        # this command has only a one in 10 chance of succeeding
        import random
        value = random.randint(0, 10)
        if value != 5:
            sdaffsfda




if __name__ == '__main__':
    import sys
    args = sys.argv[1:]
    k = args[0]
    k = jsonloads(k)
    run_batch(k)

