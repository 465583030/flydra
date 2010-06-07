import sys
from couchdb.client import Server
import subprocess
import states

def download_jobs(couch_url, db_name):
    couch_server = Server(couch_url)
    db = couch_server[db_name]

    # get all jobs that are created (not executing or complete)
    view_results = db.view('analysis/jobs')
    for row in view_results:
        if row.key != states.CREATED:
            continue

        job_id = row.id

        doc = db[job_id]
        doc['state'] = states.SUBMITTED
        
        job_depends = ','.join( doc.get('job_depends', [] ) )
        if len(job_depends):
            job_depends = '-hold_jid '+job_depends

        job_name = '-N job%s'%job_id
        # XXX use virtualenv in ~/PY
        cmd = 'qsub -b y %s %s ~/PY/bin/flydra_sge_run_job %s %s %s'%(job_name, job_depends,
                                                                      couch_url, db_name, job_id)
        subprocess.check_call(cmd,shell=True)
        db.update([doc])

def main():
    couch_url = sys.argv[1]
    db_name = sys.argv[2]
    download_jobs(couch_url, db_name)

if __name__=='__main__':
    main()
