"""Cumulative and adversarial Stage 3 HTTP contract verification."""
import copy
import concurrent.futures
from datetime import datetime,timedelta,timezone
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from verify_stage2 import Stage2, paired_fixture
from verify_contract import request,port,DAY,fixture

class Stage3(Stage2):
    def configure(self,zone='UTC'):
        f=paired_fixture(); f['restaurants'][0]['manager_user_ids']=['u1']; f['restaurants'][0]['timezone']=zone
        self.assertEqual(request(self.base,'POST','/_test/reset',f)[0],204)
        self.token=request(self.base,'POST','/auth/login',{'email':'one@example.com','password':'password1'})[1]['token']
        self.other=request(self.base,'POST','/auth/login',{'email':'two@example.com','password':'password2'})[1]['token']
        return f
    def policy(self,date=DAY,**changes):
        return dict(effective_from=date,slot_minutes=30,reservation_duration_minutes=90,cancellation_cutoff_minutes=0,
                    opening_hours=fixture()['restaurants'][0]['opening_hours'],capacities={'t1':2,'t2':4,'t3':3,'t4':4})|changes
    def publish(self,key,body=None,token=None):
        return request(self.base,'POST','/restaurants/r1/policies',body or self.policy(),token or self.token,key)
    def snapshot(self):
        return request(self.base,'GET','/_test/export')[1]
    def history(self,ref):
        return request(self.base,'GET','/reservations/'+ref+'/history',token=self.token)[1]['entries']
    def series(self,ref,key='series',**changes):
        return request(self.base,'POST','/series',dict(anchor_reference=ref,count=3,interval_weeks=1)|changes,self.token,key)
    def test_explain_independent_rules_and_strict_query(self):
        self.configure(); a=self.create('a',self.booking('t1'))[1]
        plain=request(self.base,'GET','/availability?restaurant_id=r1&date='+DAY+'&party_size=5')[1]
        self.assertTrue(all('explain' not in s for s in plain['slots']))
        explained=request(self.base,'GET','/availability?restaurant_id=r1&date='+DAY+'&party_size=5&explain=true')[1]
        slot=next(s for s in explained['slots'] if s['starts_at_local']==DAY+'T12:00')
        self.assertEqual([x['table_id'] for x in slot['explain']],['t1','t2','t3','t4'])
        self.assertEqual(slot['explain'][0]['rules'],[{'rule':'capacity','holds':False},{'rule':'no_overlap','holds':False}])
        self.assertEqual(slot['explain'][1]['rules'][1]['holds'],True)
        for value in ('false','1',''):
            self.check_error(request(self.base,'GET','/availability?restaurant_id=r1&date='+DAY+'&party_size=2&explain='+value),422,'validation_failed')
    def test_policy_bounds_authorization_retry_and_ties(self):
        self.configure()
        self.check_error(self.publish('auth',token=self.other),403,'forbidden')
        before=self.snapshot()
        for field,values in [('slot_minutes',[0,1441,True,'30']),('reservation_duration_minutes',[0,1441]),('cancellation_cutoff_minutes',[-1,10081]),('capacities',[{}, {'t1':101,'t2':4,'t3':3,'t4':4}]),('opening_hours',fixture()['restaurants'][0]['opening_hours'][:1]*2 and [fixture()['restaurants'][0]['opening_hours'][:1]*2])]:
            for value in values:
                self.check_error(self.publish('bad',self.policy(**{field:value})),422,'validation_failed')
                self.assertEqual(self.snapshot(),before)
        first=self.publish('one',self.policy('2030-01-14'))[1]
        second=self.publish('two',self.policy(DAY,reservation_duration_minutes=30))[1]
        third=self.publish('three',self.policy(DAY,reservation_duration_minutes=120))[1]
        self.assertEqual([first['policy_version'],second['policy_version'],third['policy_version']],[1,2,3])
        self.assertEqual(self.publish('three',self.policy(DAY,reservation_duration_minutes=120)),(200,third))
        self.check_error(self.publish('three',{}|{'slot_minutes':False}),409,'idempotency_key_reuse')
        a=self.create('booking')[1]; self.assertEqual(a['accepted_terms']['policy_version'],3)
        b=self.create('future',self.booking('t1','2030-01-14T12:00'))[1]; self.assertEqual(b['accepted_terms']['policy_version'],1)
        self.assertEqual(request(self.base,'GET','/restaurants/r1')[1]['reservation_duration_minutes'],60)
    def test_history_noops_pairs_revisions_owner_exceptions(self):
        self.configure(); a=self.create('pair',self.pair_body())[1]; ref=a['reference']; path='/reservations/'+ref
        entry=self.history(ref)[0]; self.assertEqual([x['field'] for x in entry['changes']],['table_ids','starts_at_local','party_size'])
        self.assertEqual(request(self.base,'PATCH',path,{'table_ids':['t1','t2']},self.token)[1],a)
        self.assertEqual(len(self.history(ref)),1)
        changed=request(self.base,'PATCH',path,{'table_id':'t4','party_size':2,'expected_revision':1},self.token)[1]
        self.assertEqual(changed['revision'],2)
        self.assertEqual([x['field'] for x in self.history(ref)[1]['changes']],['table_ids','party_size'])
        for suffix in ('history','decision'):
            self.check_error(request(self.base,'GET',path+'/'+suffix),404,'not_found')
            self.check_error(request(self.base,'GET',path+'/'+suffix,token=self.other),404,'not_found')
        self.check_error(request(self.base,'PATCH',path,{'expected_revision':1,'party_size':False},self.token),409,'stale_revision')
        cancelled=request(self.base,'POST',path+'/cancel',{},self.token)[1]; self.assertEqual(cancelled['revision'],3)
        self.assertEqual(request(self.base,'POST',path+'/cancel',{},self.token)[1],cancelled)
        self.assertEqual([e['seq'] for e in self.history(ref)],[1,2,3]); self.assertEqual(self.history(ref)[-1]['changes'],[])
        self.assertEqual(self.create('pair',self.pair_body()),(200,a))
    def test_frozen_terms_policy_noop_real_amendment_and_cutoff(self):
        self.configure(); a=self.create('a')[1]; path='/reservations/'+a['reference']; terms=a['accepted_terms']
        self.publish('p',self.policy(capacities={'t1':1,'t2':4,'t3':3,'t4':4}))
        self.assertEqual(request(self.base,'PATCH',path,{},self.token)[1],a)
        self.check_error(request(self.base,'PATCH',path,{'starts_at_local':DAY+'T13:00'},self.token),422,'party_exceeds_capacity')
        changed=request(self.base,'PATCH',path,{'table_id':'t2'},self.token)[1]
        self.assertEqual(changed['accepted_terms']['policy_version'],1); self.assertEqual(changed['revision'],2)
        self.assertEqual(self.history(a['reference'])[0]['accepted_terms'],terms)
        self.assertNotEqual(changed['ends_at'],a['ends_at'])
        # Publication cannot relax an existing near-start cutoff.
        f=self.configure(); near=(datetime.now(timezone.utc)+timedelta(minutes=60)).replace(second=0,microsecond=0)
        near=near.replace(minute=(near.minute//30)*30)
        b=self.create('near',self.booking('t1',near.isoformat(timespec='minutes')[:16]))[1]
        self.publish('relax',self.policy(near.date().isoformat()))
        self.check_error(request(self.base,'POST','/reservations/'+b['reference']+'/cancel',{},self.token),409,'cutoff_passed')
    def test_expected_revision_concurrent_linearization(self):
        self.configure(); a=self.create('a')[1]; path='/reservations/'+a['reference']
        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
            replies=list(pool.map(lambda i:request(self.base,'PATCH',path,{'party_size':1,'expected_revision':1},self.token),range(50)))
        self.assertEqual(sum(s==200 for s,_ in replies),1)
        self.assertEqual(sum(s==409 and b['error']['code']=='stale_revision' for s,b in replies),49)
        self.assertEqual(len(self.history(a['reference'])),2)
    def test_series_anchor_frozen_policy_generation_and_exception_counters(self):
        self.configure(); a=self.create('a')[1]
        self.publish('later',self.policy('2030-01-14',reservation_duration_minutes=120))
        before_anchor=copy.deepcopy(a); history=self.history(a['reference'])
        result=self.series(a['reference'])[1]; sid=result['series_id']; self.assertEqual(result['revision'],1)
        self.assertEqual(result['occurrences'][0]['reservation'],before_anchor)
        self.assertEqual(self.history(a['reference']),history)
        self.assertEqual([o['reservation']['accepted_terms']['policy_version'] for o in result['occurrences']],[0,1,1])
        ref=result['occurrences'][1]['reference']; path='/reservations/'+ref
        request(self.base,'PATCH',path,{},self.token)
        self.assertEqual(request(self.base,'GET','/series/'+sid,token=self.token)[1],result)
        request(self.base,'PATCH',path,{'party_size':1},self.token)
        current=request(self.base,'GET','/series/'+sid,token=self.token)[1]
        self.assertEqual(current['revision'],2); self.assertTrue(current['occurrences'][1]['exception'])
        request(self.base,'POST','/reservations/'+a['reference']+'/cancel',{},self.token)
        current=request(self.base,'GET','/series/'+sid,token=self.token)[1]
        self.assertEqual(current['revision'],3); self.assertFalse(current['occurrences'][0]['exception'])
        self.assertEqual(current['occurrences'][2]['reservation']['status'],'confirmed')
        self.assertEqual(self.series(a['reference']),(200,result))
        for token in (None,self.other):
            self.check_error(request(self.base,'GET','/series/'+sid,token=token),404,'not_found')
    def test_series_first_failure_atomic_rollback_key_reuse(self):
        self.configure(); anchor=self.create('a')[1]
        self.create('block',self.booking('t1','2030-01-21T12:00'))
        before=self.snapshot()
        self.check_error(self.series(anchor['reference']),409,'table_unavailable')
        self.assertEqual(self.snapshot(),before)
        self.assertEqual(self.series(anchor['reference'],count=2)[0],201)
        self.check_error(self.series(anchor['reference'],'new'),409,'already_in_series')
    def test_series_dst_gap_fold_both_zones(self):
        for zone,anchor,gap in [('Europe/Berlin','2026-03-22T02:30','2026-03-29'),('America/New_York','2026-03-01T02:30','2026-03-08')]:
            self.configure(zone); a=self.create('a',self.booking('t1',anchor))[1]
            # Past cutoffs cannot be bypassed: use fixture snapshot clock-independent future equivalent year.
            if request(self.base,'POST','/series',{'anchor_reference':a['reference'],'count':2,'interval_weeks':1},self.token,'gap')[1]['error']['code']=='cutoff_passed':
                future={'Europe/Berlin':'2030-03-24T02:30','America/New_York':'2030-03-03T02:30'}[zone]
                self.configure(zone); a=self.create('f',self.booking('t1',future))[1]
                before=self.snapshot(); self.check_error(self.series(a['reference'],count=2),422,'invalid_local_time'); self.assertEqual(self.snapshot(),before)
        for zone,anchor in [('Europe/Berlin','2030-10-20T02:30'),('America/New_York','2030-10-27T01:30')]:
            self.configure(zone); a=self.create('f',self.booking('t1',anchor))[1]
            s=self.series(a['reference'],count=2)[1]
            generated=s['occurrences'][1]['reservation']
            self.assertTrue(generated['starts_at'].endswith('+02:00' if zone=='Europe/Berlin' else '-04:00'))
            self.assertEqual((datetime.fromisoformat(generated['ends_at'])-datetime.fromisoformat(generated['starts_at'])).total_seconds(),3600)
    def test_collective_series_changes_policy_atomicity_and_replay(self):
        self.configure(); anchor=self.create('a')[1]; s=self.series(anchor['reference'])[1]; refs=[o['reference'] for o in s['occurrences']]
        body={'moves':[{'reference':ref,'table_id':'t2','expected_revision':1} for ref in refs]}
        before=self.snapshot(); moved=request(self.base,'POST','/reservation-moves',body,self.token,'batch')
        self.assertEqual(moved[0],201); after=self.snapshot()
        self.assertEqual(after['state']['series'][s['series_id']]['revision'],2)
        self.assertEqual(after['state']['restaurant_revisions']['r1'],before['state']['restaurant_revisions']['r1']+1)
        self.assertEqual(len(after['state']['series'][s['series_id']]['exceptions']),3)
        self.assertEqual(request(self.base,'POST','/reservation-moves',body,self.token,'batch'),(200,moved[1]))
        self.assertEqual(self.snapshot(),after)
        failed={'moves':[{'reference':refs[0],'party_size':3},{'reference':refs[1],'expected_revision':1}]}
        self.check_error(request(self.base,'POST','/reservation-moves',failed,self.token,'fail'),409,'stale_revision')
        self.assertEqual(self.snapshot(),after)
        self.assertEqual(request(self.dest,'POST','/_test/import',after)[0],204)
        self.assertEqual(request(self.dest,'GET','/series/'+s['series_id'],token=self.token)[1],request(self.base,'GET','/series/'+s['series_id'],token=self.token)[1])
    def test_actual_stage2_import_adoption_preserves_legacy_receipt(self):
        number=port(); legacy=Path('/stage-2/app.py')
        if not legacy.exists(): legacy=Path(__file__).parent.parent/'stage-2'/'app.py'
        p=subprocess.Popen([sys.executable,str(legacy)],env=dict(os.environ,PORT=str(number),PYTHONDONTWRITEBYTECODE='1'),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        base=f'http://127.0.0.1:{number}'
        try:
            for _ in range(100):
                try:
                    if request(base,'GET','/health')[0]==200: break
                except OSError: time.sleep(.05)
            request(base,'POST','/_test/reset',paired_fixture())
            token=request(base,'POST','/auth/login',{'email':'one@example.com','password':'password1'})[1]['token']
            body=self.pair_body(); original=request(base,'POST','/reservations',body,token,'legacy')[1]
            snapshot=request(base,'GET','/_test/export')[1]
            self.assertEqual(request(self.base,'POST','/_test/import',snapshot)[0],204)
            adopted=request(self.base,'POST','/series',{'anchor_reference':original['reference'],'count':2,'interval_weeks':1},token,'adopt')
            self.assertEqual(adopted[0],201)
            self.assertEqual(request(self.base,'POST','/reservations',body,token,'legacy'),(200,original))
            self.assertEqual(adopted[1]['occurrences'][0]['reservation']['accepted_terms']['policy_version'],0)
        finally: p.terminate(); p.wait(timeout=5)

    def test_concurrent_policy_and_series_receipts_atomic_snapshots(self):
        self.configure()
        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
            policies=list(pool.map(lambda _:self.publish('concurrent'),range(50)))
        self.assertEqual([status for status,_ in policies].count(201),1)
        self.assertEqual([status for status,_ in policies].count(200),49)
        self.assertTrue(all(value==policies[0][1] for _,value in policies))
        a=self.create('a')[1]
        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
            results=list(pool.map(lambda _:self.series(a['reference']),range(50)))
        self.assertEqual([status for status,_ in results].count(201),1)
        self.assertEqual([status for status,_ in results].count(200),49)
        self.assertTrue(all(value==results[0][1] for _,value in results))
        self.assertEqual(len(self.snapshot()['state']['reservations']),3)
        original=self.snapshot()
        for field,values in [('count',[None,True,1,13,'2',2.5]),('interval_weeks',[None,False,0,5,'1',1.5])]:
            for value in values:
                # Fresh unadopted anchor lets shape validation execute.
                self.configure(); anchor=self.create('fresh')[1]; before=self.snapshot()
                self.check_error(self.series(anchor['reference'],**{field:value}),422,'validation_failed')
                self.assertEqual(self.snapshot(),before)
        self.assertEqual(request(self.base,'POST','/_test/import',original)[0],204)
        bad=copy.deepcopy(original); ref=a['reference']; bad['state']['histories'][ref][0]['seq']=2
        self.check_error(request(self.base,'POST','/_test/import',bad),422,'validation_failed')
        self.assertEqual(self.snapshot(),original)

    def test_corrupt_creation_receipt_revisions_import_rolls_back(self):
        self.configure(); body=self.booking(); original=self.create('receipt',body)[1]
        snapshot=self.snapshot()
        for value in (0,-1,True,False,'1',None,1.5,2,999):
            bad=copy.deepcopy(snapshot); bad['state']['receipts'][0]['response']['revision']=value
            self.check_error(request(self.base,'POST','/_test/import',bad),422,'validation_failed')
            self.assertEqual(self.snapshot(),snapshot)
            self.assertEqual(self.create('receipt',body),(200,original))
        for missing in ('revision','accepted_terms'):
            bad=copy.deepcopy(snapshot);del bad['state']['receipts'][0]['response'][missing]
            self.check_error(request(self.base,'POST','/_test/import',bad),422,'validation_failed')
            self.assertEqual(self.snapshot(),snapshot)
        # Frozen creation revision and terms remain legitimate after newer edits/policies.
        self.publish('future',self.policy(reservation_duration_minutes=120))
        request(self.base,'PATCH','/reservations/'+original['reference'],{'table_id':'t2'},self.token)
        request(self.base,'POST','/reservations/'+original['reference']+'/cancel',{},self.token)
        changed=self.snapshot()
        self.assertEqual(request(self.dest,'POST','/_test/import',changed)[0],204)
        self.assertEqual(request(self.dest,'POST','/reservations',body,self.token,'receipt'),(200,original))
        self.assertEqual(request(self.dest,'GET','/_test/export')[1],changed)
    def test_corrupt_batch_and_series_receipts_rejected_frozen_receipts_preserved(self):
        self.configure(); original=self.create('pair',self.pair_body())[1]
        batch={'moves':[{'reference':original['reference'],'table_ids':['t3','t4']}]}
        moved=request(self.base,'POST','/reservation-moves',batch,self.token,'moved')[1]
        adopted=self.series(original['reference'])[1]
        # Change and cancel current state without rewriting saved batch/series receipts.
        request(self.base,'PATCH','/reservations/'+original['reference'],{'party_size':6},self.token)
        request(self.base,'POST','/reservations/'+adopted['occurrences'][1]['reference']+'/cancel',{},self.token)
        snapshot=self.snapshot()
        self.assertEqual(request(self.dest,'POST','/_test/import',snapshot)[0],204)
        self.assertEqual(request(self.dest,'POST','/reservation-moves',batch,self.token,'moved'),(200,moved))
        self.assertEqual(request(self.dest,'POST','/series',{'anchor_reference':original['reference'],'count':3,'interval_weeks':1},self.token,'series'),(200,adopted))
        for path in ('/reservation-moves','/series'):
            index=next(i for i,r in enumerate(snapshot['state']['receipts']) if r['path']==path)
            for value in (0,-1,True,'1',1.5,999):
                bad=copy.deepcopy(snapshot);response=bad['state']['receipts'][index]['response']
                if path=='/reservation-moves': response['reservations'][0]['revision']=value
                else: response['revision']=value
                self.check_error(request(self.base,'POST','/_test/import',bad),422,'validation_failed')
                self.assertEqual(self.snapshot(),snapshot)
            bad=copy.deepcopy(snapshot); response=bad['state']['receipts'][index]['response']
            item=response['reservations'][0] if path=='/reservation-moves' else response['occurrences'][1]['reservation']
            item['revision']=0
            self.check_error(request(self.base,'POST','/_test/import',bad),422,'validation_failed')
            self.assertEqual(self.snapshot(),snapshot)
            bad=copy.deepcopy(snapshot);response=bad['state']['receipts'][index]['response']
            item=response['reservations'][0] if path=='/reservation-moves' else response['occurrences'][0]['reservation']
            item['accepted_terms']['policy_version']=True
            self.check_error(request(self.base,'POST','/_test/import',bad),422,'validation_failed')
            self.assertEqual(self.snapshot(),snapshot)
    def test_corrupt_policy_receipt_and_historical_payload_types(self):
        self.configure(); published=self.publish('p')[1]; original=self.create('booking')[1]; snapshot=self.snapshot()
        policy_index=next(i for i,r in enumerate(snapshot['state']['receipts']) if r['path'].endswith('/policies'))
        booking_index=next(i for i,r in enumerate(snapshot['state']['receipts']) if r['path']=='/reservations')
        for value in (0,True,'1',1.5,2):
            bad=copy.deepcopy(snapshot);bad['state']['receipts'][policy_index]['response']['policy_version']=value
            self.check_error(request(self.base,'POST','/_test/import',bad),422,'validation_failed');self.assertEqual(self.snapshot(),snapshot)
        for field,value in [('party_size',1),('starts_at_local',DAY+'T13:00'),('table_ids',['t2'])]:
            bad=copy.deepcopy(snapshot);bad['state']['receipts'][booking_index]['response'][field]=value
            self.check_error(request(self.base,'POST','/_test/import',bad),422,'validation_failed');self.assertEqual(self.snapshot(),snapshot)
        bad=copy.deepcopy(snapshot);bad['state']['receipts'][booking_index]['body']['party_size']=1
        self.check_error(request(self.base,'POST','/_test/import',bad),422,'validation_failed');self.assertEqual(self.snapshot(),snapshot)
        bad=copy.deepcopy(snapshot);bad['state']['receipts'][policy_index]['body']['slot_minutes']=60
        self.check_error(request(self.base,'POST','/_test/import',bad),422,'validation_failed');self.assertEqual(self.snapshot(),snapshot)
        bad=copy.deepcopy(snapshot);bad['state']['policies']['r1'][0]['policy_version']=True
        self.check_error(request(self.base,'POST','/_test/import',bad),422,'validation_failed');self.assertEqual(self.snapshot(),snapshot)
        self.assertEqual(request(self.dest,'POST','/_test/import',snapshot)[0],204)
        self.assertEqual(request(self.dest,'POST','/restaurants/r1/policies',self.policy(),self.token,'p'),(200,published))

    def test_legacy_unknown_fields_receipts_survive_second_import(self):
        for source in ('stage-1','stage-2'):
            number=port(); app=Path('/'+source+'/app.py')
            if not app.exists(): app=Path(__file__).parent.parent/source/'app.py'
            process=subprocess.Popen([sys.executable,str(app)],env=dict(os.environ,PORT=str(number),PYTHONDONTWRITEBYTECODE='1'),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            base=f'http://127.0.0.1:{number}'
            try:
                for _ in range(100):
                    try:
                        if request(base,'GET','/health')[0]==200: break
                    except OSError: time.sleep(.05)
                request(base,'POST','/_test/reset',fixture())
                token=request(base,'POST','/auth/login',{'email':'one@example.com','password':'password1'})[1]['token']
                body=self.booking(expected_revision=True)
                if source=='stage-1': body['table_ids']='ignored-before-stage-2'
                original=request(base,'POST','/reservations',body,token,'old-create')[1]
                batch={'moves':[{'reference':original['reference'],'table_id':'t2','expected_revision':True}]}
                moved=request(base,'POST','/reservation-moves',batch,token,'old-batch')[1]
                exported=request(base,'GET','/_test/export')[1]
                self.assertEqual(request(self.base,'POST','/_test/import',exported)[0],204)
                migrated=self.snapshot()
                self.assertEqual(request(self.dest,'POST','/_test/import',migrated)[0],204)
                self.assertEqual(request(self.dest,'POST','/reservations',body,token,'old-create'),(200,original))
                self.assertEqual(request(self.dest,'POST','/reservation-moves',batch,token,'old-batch'),(200,moved))
            finally: process.terminate();process.wait(timeout=5)

if __name__=='__main__': unittest.main(defaultTest='Stage3',verbosity=2)
