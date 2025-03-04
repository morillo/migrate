import os

from dbclient import *


class JobsClient(ClustersClient):

    def get_jobs_default_cluster_conf(self):
        if self.is_aws():
            cluster_json_file = 'data/default_jobs_cluster_aws.json'
        else:
            cluster_json_file = 'data/default_jobs_cluster_azure.json'
        with open(cluster_json_file, 'r') as fp:
            cluster_json = json.loads(fp.read())
            return cluster_json

    def get_jobs_list(self, print_json=False):
        """ 
        Returns an array of json objects for jobs. It might contain jobs in SINGLE_TASK and 
        MULTI_TASK format. 
        """
        jobsById = {}
        # fetch all jobs using API 2.0. The 'format' field of each job can either be SINGLE_TASK 
        # or MULTI_TASK. MULTI_TASK jobs, however, are returned without task definitions (the 
        # 'tasks' field) on API 2.0.
        res = self.get("/jobs/list", print_json, version='2.0')
        for job in res.get('jobs', []):
            jobsById[job.get('job_id')] = job

        limit = 25 # max limit supported by the API
        offset = 0
        has_more = True
        # fetch all jobs again, this time using API 2.1, in order to get MULTI_TASK jobs with 
        # task definitions. Note that the 'format' field will be set as MULTI_TASK for all jobs 
        # and the 'tasks' field will be present for all jobs as well
        while has_more:
            res = self.get(f'/jobs/list?expand_tasks=true&offset={offset}&limit={limit}', print_json, version='2.1')
            offset += limit
            has_more = res.get('has_more')
            for job in res.get('jobs', []):
                jobId = job.get('job_id')
                # only replaces "real" MULTI_TASK jobs, as they contain the task definitions.
                if jobsById[jobId].get('format') == 'MULTI_TASK':
                    jobsById[jobId] = job
        return jobsById.values()

    def get_job_id_by_name(self):
        """
        get a dict mapping of job name to job id for the new job ids
        :return:
        """
        jobs = self.get_jobs_list()
        job_ids = {}
        for job in jobs:
            if(not job_ids[job['settings']['name']]):
                job_ids[job['settings']['name']] = []
            job_ids[job['settings']['name']].append(job['job_id'])
        return job_ids

    def update_imported_job_names(self):
        # loop through and update the job names to remove the custom delimiter + job_id suffix
        current_jobs_list = self.get_jobs_list()
        for job in current_jobs_list:
            job_id = job['job_id']
            job_name = job['settings']['name']
            # job name was set to `old_job_name:::{job_id}` to support duplicate job names
            # we need to parse the old job name and update the current jobs
            old_job_name = job_name.split(':::')[0]
            new_settings = {'name': old_job_name}
            update_args = {'job_id': job_id, 'new_settings': new_settings}
            print('Updating job name:', update_args)
            resp = self.post('/jobs/update', update_args)
            print(resp)

    def log_job_configs(self, users_list=[], log_file='jobs.log', acl_file='acl_jobs.log'):
        """
        log all job configs and the ACLs for each job
        :param users_list: a list of users / emails to filter the results upon (optional for group exports)
        :param log_file: log file to store job configs as json entries per line
        :param acl_file: log file to store job ACLs
        :return:
        """
        jobs_log = self.get_export_dir() + log_file
        acl_jobs_log = self.get_export_dir() + acl_file
        # pinned by cluster_user is a flag per cluster
        jl_full = self.get_jobs_list(False)
        if users_list:
            # filter the jobs list to only contain users that exist within this list
            jl = list(filter(lambda x: x.get('creator_user_name', '') in users_list, jl_full))
        else:
            jl = jl_full
        with open(jobs_log, "w") as log_fp, open(acl_jobs_log, 'w') as acl_fp:
            for x in jl:
                job_id = x['job_id']
                new_job_name = x['settings']['name'] + ':::' + str(job_id)
                # grab the settings obj
                job_settings = x['settings']
                # update the job name
                job_settings['name'] = new_job_name
                # reset the original struct with the new settings
                x['settings'] = job_settings
                log_fp.write(json.dumps(x) + '\n')
                job_perms = self.get(f'/preview/permissions/jobs/{job_id}')
                job_perms['job_name'] = new_job_name
                acl_fp.write(json.dumps(job_perms) + '\n')

    def import_job_configs(self, log_file='jobs.log', acl_file='acl_jobs.log', replace_jobs=False):
        jobs_log = self.get_export_dir() + log_file
        acl_jobs_log = self.get_export_dir() + acl_file
        if not os.path.exists(jobs_log):
            print("No job configurations to import.")
            return
        # get an old cluster id to new cluster id mapping object
        cluster_mapping = self.get_cluster_id_mapping()
        old_2_new_policy_ids = self.get_new_policy_id_dict()  # dict { old_policy_id : new_policy_id }
        jobs_to_replace = []

        def adjust_ids_for_cluster(settings): #job_settings or task_settings
            if 'existing_cluster_id' in settings:
                old_cid = settings['existing_cluster_id']
                # set new cluster id for existing cluster attribute
                new_cid = cluster_mapping.get(old_cid, None)
                if not new_cid:
                    print("Existing cluster has been removed. Resetting job to use new cluster.")
                    settings.pop('existing_cluster_id')
                    settings['new_cluster'] = self.get_jobs_default_cluster_conf()
                else:
                    settings['existing_cluster_id'] = new_cid
            else:  # new cluster config
                cluster_conf = settings['new_cluster']
                if 'policy_id' in cluster_conf:
                    old_policy_id = cluster_conf['policy_id']
                    cluster_conf['policy_id'] = old_2_new_policy_ids[old_policy_id]
                # check for instance pools and modify cluster attributes
                if 'instance_pool_id' in cluster_conf:
                    new_cluster_conf = self.cleanup_cluster_pool_configs(cluster_conf, job_creator, True)
                else:
                    new_cluster_conf = cluster_conf
                settings['new_cluster'] = new_cluster_conf

        with open(jobs_log, 'r') as fp:
            for line in fp:
                job_conf = json.loads(line)
                job_creator = job_conf.get('creator_user_name', '')
                job_settings = job_conf['settings']
                job_schedule = job_settings.get('schedule', None)
                if job_schedule:
                    # set all imported jobs as paused
                    job_schedule['pause_status'] = 'PAUSED'
                    job_settings['schedule'] = job_schedule
                if 'format' not in job_settings or job_settings.get('format') == 'SINGLE_TASK':
                    adjust_ids_for_cluster(job_settings)
                else:
                    for task_settings in job_settings.get('tasks', []):
                        adjust_ids_for_cluster(task_settings)

                print("Current Job Name: {0}".format(job_conf['settings']['name']))
                # creator can be none if the user is no longer in the org. see our docs page
                creator_user_name = job_conf.get('creator_user_name', None)
                create_resp = self.post('/jobs/create', job_settings)
                if 'error_code' in create_resp:
                    print("Resetting job to use default cluster configs due to expired configurations.")
                    job_settings['new_cluster'] = self.get_jobs_default_cluster_conf()
                    create_resp_retry = self.post('/jobs/create', job_settings)
                # remove old job
                if(replace_jobs):
                    jobs_to_replace.append(job_settings['name'].split(':::')[0])
        # update the jobs with their ACLs
        job_id_by_name = self.get_job_id_by_name()
        with open(acl_jobs_log, 'r') as acl_fp:
            for line in acl_fp:
                acl_conf = json.loads(line)
                current_job_id = job_id_by_name[acl_conf['job_name']][0]
                job_path = f'jobs/{current_job_id}'  # contains `/jobs/{job_id}` path
                api = f'/preview/permissions/{job_path}'
                # get acl permissions for jobs
                acl_perms = self.build_acl_args(acl_conf['access_control_list'], True)
                acl_create_args = {'access_control_list': acl_perms}
                acl_resp = self.patch(api, acl_create_args)
                print(acl_resp)
        if(replace_jobs):
            # remove old jobs
            for job_name in jobs_to_replace:
                for job_id in job_id_by_name[job_name]:
                    self.post('/jobs/delete', {'job_id': job_id})
        # update the imported job names
        self.update_imported_job_names()

    def pause_all_jobs(self, pause=True):
        job_list = self.get_jobs_list()
        for job_conf in job_list:
            job_settings = job_conf['settings']
            job_schedule = job_settings.get('schedule', None)
            if job_schedule:
                # set all imported jobs as paused or un-paused
                if pause:
                    job_schedule['pause_status'] = 'PAUSED'
                else:
                    job_schedule['pause_status'] = 'UNPAUSED'
                job_settings['schedule'] = job_schedule
                update_job_conf = {'job_id': job_conf['job_id'],
                                   'new_settings': job_settings}
                update_job_resp = self.post('/jobs/reset', update_job_conf)

    def delete_all_jobs(self):
        job_list = self.get('/jobs/list').get('jobs', [])
        for job in job_list:
            self.post('/jobs/delete', {'job_id': job['job_id']})

    def get_cluster_id_mapping(self, log_file='clusters.log'):
        """
        Get a dict mapping of old cluster ids to new cluster ids for jobs connecting to existing clusters
        :param log_file:
        :return:
        """
        cluster_logfile = self.get_export_dir() + log_file
        current_cl = self.get('/clusters/list').get('clusters', [])
        old_clusters = {}
        # build dict with old cluster name to cluster id mapping
        if not os.path.exists(cluster_logfile):
            raise ValueError('Clusters log must exist to map clusters to previous existing cluster ids')
        with open(cluster_logfile, 'r') as fp:
            for line in fp:
                conf = json.loads(line)
                old_clusters[conf['cluster_name']] = conf['cluster_id']
        new_to_old_mapping = {}
        for new_cluster in current_cl:
            old_cluster_id = old_clusters.get(new_cluster['cluster_name'], None)
            if old_cluster_id:
                new_to_old_mapping[old_cluster_id] = new_cluster['cluster_id']
        return new_to_old_mapping
