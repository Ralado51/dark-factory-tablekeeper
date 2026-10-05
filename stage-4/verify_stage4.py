"""Cumulative final-stage API and exact optimization invariants."""
import copy
import concurrent.futures
from itertools import product
from datetime import datetime,timedelta,timezone
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from verify_stage3 import Stage3
from verify_stage2 import paired_fixture
from verify_contract import request,fixture,DAY,port

class Stage4(Stage3):
    def replan(self,key='plan',table='t1',start=DAY+'T12:00:00+00:00',end=DAY+'T14:00:00+00:00',token=None,rid='r1'):
        return request(self.base,'POST','/restaurants/'+rid+'/replans',{'table_id':table,'from':start,'to':end},token or self.token,key)
    def apply(self,plan,key='apply',token=None,rid='r1'):
        return request(self.base,'POST','/restaurants/'+rid+'/replans/'+plan['plan_id']+'/apply',{},token or self.token,key)
    def amend_series(self,s,key='amend',**changes):
        return request(self.base,'POST','/series/'+s['series_id']+'/amend',dict(expected_revision=s['revision'],from_index=0,local_time='14:00')|changes,self.token,key)
    def test_preview_apply_closure_history_revision_and_frozen_terms(self):
        self.configure(); a=self.create('a')[1]; b=self.create('b',self.booking('t2'))[1]
        before=self.snapshot();plan=self.replan()[1];after=self.snapshot()
        for field in ('reservations','histories','restaurant_revisions','closures','series'):
            self.assertEqual(after['state'][field],before['state'][field])
        self.assertEqual(plan['restaurant_revision'],2);self.assertEqual(plan['moved_count'],1)
        self.assertEqual([x['reference'] for x in plan['assignments']],sorted([a['reference'],b['reference']]))
        applied=self.apply(plan);self.assertEqual(applied[0],201)
        self.assertEqual(applied[1]['restaurant_revision'],3)
        records={r['reference']:r for r in applied[1]['reservations']}
        self.assertEqual(records[b['reference']],b)
        moved=records[a['reference']];self.assertEqual(moved['revision'],2)
        for field in ('starts_at','ends_at','starts_at_local','party_size','accepted_terms','reservation_id','reference','created_at'):
            self.assertEqual(moved[field],a[field])
        event=self.history(a['reference'])[-1];self.assertEqual(event['event'],'reassigned');self.assertEqual(event['plan_id'],plan['plan_id'])
        self.assertEqual(event['changes'],[{'field':'table_ids','from':['t1'],'to':moved['table_ids']}])
        self.assertEqual(self.apply(plan),(200,applied[1]))
        self.check_error(self.apply(plan,'different'),409,'plan_already_applied')
        self.assertEqual(self.create('a'),(200,a))
        self.check_error(self.create('blocked',self.booking('t1',DAY+'T13:00')),409,'table_unavailable')
        self.assertEqual(self.create('adjacent',self.booking('t1',DAY+'T14:00'))[0],201)
        self.assertEqual(self.create('before',self.booking('t1',DAY+'T11:00'))[0],201)
        explained=request(self.base,'GET','/availability?restaurant_id=r1&date='+DAY+'&party_size=3&explain=true')[1]
        slot=next(s for s in explained['slots'] if s['starts_at_local']==DAY+'T13:00')
        first=slot['explain'][0];self.assertEqual(first['rules'],[{'rule':'capacity','holds':False},{'rule':'no_overlap','holds':False}])
        snapshot=self.snapshot();self.assertEqual(request(self.dest,'POST','/_test/import',snapshot)[0],204)
        self.assertEqual(request(self.dest,'POST','/restaurants/r1/replans/'+plan['plan_id']+'/apply',{},self.token,'apply'),(200,applied[1]))
    def test_exact_optimizer_against_independent_bruteforce(self):
        self.configure(); f=paired_fixture(); f['restaurants'][0]['manager_user_ids']=['u1']
        f['reservations']=[{'id':'a','reference':'BOOK01','user_id':'u1',**self.booking('t1')},
                           {'id':'b','reference':'BOOK02','user_id':'u1',**self.booking('t2')},
                           {'id':'c','reference':'BOOK03','user_id':'u1',**self.booking('t3',DAY+'T13:00')},
                           {'id':'fixed','reference':'FIXED01','user_id':'u1',**self.booking('t4',DAY+'T14:00')}]
        request(self.base,'POST','/_test/reset',f);self.token=request(self.base,'POST','/auth/login',{'email':'one@example.com','password':'password1'})[1]['token']
        plan=self.replan()[1]
        records=[r for r in request(self.base,'GET','/reservations',token=self.token)[1]['reservations'] if r['reference'].startswith('BOOK')]
        records.sort(key=lambda r:r['reference']);r=f['restaurants'][0]
        options=[[t['id']] for t in r['tables']]+r['combinable'];capacities={t['id']:t['capacity'] for t in r['tables']}
        best=None;best_ids=None
        for ranks in product(range(len(options)),repeat=3):
            ids=[options[i] for i in ranks]
            if any('t1' in choice or sum(capacities[t] for t in choice)<b['party_size'] for choice,b in zip(ids,records)):continue
            good=True
            for i in range(3):
                for j in range(i):
                    overlap=records[i]['starts_at']<records[j]['ends_at'] and records[j]['starts_at']<records[i]['ends_at']
                    if overlap and set(ids[i])&set(ids[j]):good=False
            if not good:continue
            score=(sum(set(choice)!=set(b['table_ids']) for choice,b in zip(ids,records)),sum(sum(capacities[t] for t in choice)-b['party_size'] for choice,b in zip(ids,records)),ranks)
            if best is None or score<best:best=score;best_ids=ids
        self.assertEqual([a['table_ids'] for a in plan['assignments']],best_ids)
        self.assertEqual((plan['moved_count'],plan['unused_seats']),best[:2])
    def test_minimal_changes_precede_unused_seats_and_pair_rank(self):
        self.configure();self.create('pair',self.pair_body(size=2))
        plan=self.replan(table='t4')[1]
        self.assertEqual(plan['moved_count'],0)
        self.assertEqual(plan['assignments'][0]['table_ids'],['t2','t1'])
        self.assertEqual(plan['unused_seats'],4)
        # Accepted capacity, not a subsequently reduced policy, controls repairs.
        self.publish('reduce',self.policy(capacities={'t1':1,'t2':1,'t3':1,'t4':1}))
        moved=self.replan('repair',table='t1')[1]
        self.assertEqual(moved['moved_count'],1)
        self.assertEqual(self.apply(moved)[0],201)
    def test_stale_isolation_authorization_no_feasible_and_limits(self):
        self.configure();self.create('a');plan=self.replan()[1]
        self.check_error(self.replan('unauthorized',token=self.other),403,'forbidden')
        self.publish('p');before=self.snapshot();self.check_error(self.apply(plan),409,'stale_plan');self.assertEqual(self.snapshot(),before)
        self.configure();f=fixture();f['restaurants'][0]['manager_user_ids']=['u1'];f['restaurants'][0]['tables']=f['restaurants'][0]['tables'][:1]
        request(self.base,'POST','/_test/reset',f);self.token=request(self.base,'POST','/auth/login',{'email':'one@example.com','password':'password1'})[1]['token']
        self.create('a');before=self.snapshot();self.check_error(self.replan(),409,'no_feasible_plan');self.assertEqual(self.snapshot(),before)
        f=self.configure();f['restaurants'][0]['tables'] += [{'id':'limit'+str(i),'label':'L','capacity':2} for i in range(3)]
        request(self.base,'POST','/_test/reset',f);self.token=request(self.base,'POST','/auth/login',{'email':'one@example.com','password':'password1'})[1]['token']
        self.check_error(self.replan(),422,'planning_limit')
        f=self.configure();other=copy.deepcopy(f['restaurants'][0]);other['id']='r2';f['restaurants'].append(other)
        request(self.base,'POST','/_test/reset',f);self.token=request(self.base,'POST','/auth/login',{'email':'one@example.com','password':'password1'})[1]['token']
        self.create('a');plan=self.replan()[1]
        p2=self.replan('other',rid='r2')[1];self.assertEqual(self.apply(p2,'other-apply',rid='r2')[0],201)
        self.assertEqual(self.apply(plan)[0],201)
    def test_50_concurrent_apply_exactly_once_and_frozen_replay(self):
        self.configure();self.create('a');plan=self.replan()[1]
        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool: results=list(pool.map(lambda _:self.apply(plan),range(50)))
        self.assertEqual([s for s,_ in results].count(201),1);self.assertEqual([s for s,_ in results].count(200),49)
        self.assertTrue(all(b==results[0][1] for _,b in results))
        self.assertEqual(len(self.snapshot()['state']['closures']['r1']),1)
        self.assertEqual(self.snapshot()['state']['restaurant_revisions']['r1'],2)
        ref=results[0][1]['reservations'][0]['reference'];request(self.base,'POST','/reservations/'+ref+'/cancel',{},self.token)
        self.assertEqual(self.apply(plan),(200,results[0][1]))
    def test_series_amend_scheduled_dates_exceptions_noops_counters_and_import(self):
        self.configure();a=self.create('a')[1];s=self.series(a['reference'])[1]
        refs=[o['reference'] for o in s['occurrences']]
        request(self.base,'PATCH','/reservations/'+refs[1],{'starts_at_local':'2030-01-15T15:00'},self.token)
        request(self.base,'POST','/reservations/'+refs[2]+'/cancel',{},self.token)
        s=request(self.base,'GET','/series/'+s['series_id'],token=self.token)[1];before=self.snapshot()
        changed=self.amend_series(s)[1]
        self.assertEqual(changed['revision'],s['revision']+1)
        self.assertEqual(changed['occurrences'][0]['reservation']['starts_at_local'],DAY+'T14:00')
        self.assertEqual(changed['occurrences'][1],s['occurrences'][1]);self.assertEqual(changed['occurrences'][2],s['occurrences'][2])
        self.assertFalse(changed['occurrences'][0]['exception'])
        after=self.snapshot();self.assertEqual(after['state']['restaurant_revisions']['r1'],before['state']['restaurant_revisions']['r1']+1)
        self.assertEqual(self.amend_series(s),(200,changed))
        noop=self.amend_series(changed,'noop')[1];self.assertEqual(noop,changed)
        self.assertEqual(self.snapshot()['state']['restaurant_revisions'],after['state']['restaurant_revisions'])
        empty=self.amend_series(changed,'empty',from_index=1)[1];self.assertEqual(empty,changed)
        snapshot=self.snapshot();self.assertEqual(request(self.dest,'POST','/_test/import',snapshot)[0],204)
        self.assertEqual(request(self.dest,'POST','/series/'+s['series_id']+'/amend',{'expected_revision':s['revision'],'from_index':0,'local_time':'14:00'},self.token,'amend'),(200,changed))
    def test_series_repair_preserves_schedule_exception_flags_and_accepted_terms(self):
        self.configure();a=self.create('a')[1];s=self.series(a['reference'])[1]
        request(self.base,'PATCH','/reservations/'+s['occurrences'][1]['reference'],{'party_size':1},self.token)
        s=request(self.base,'GET','/series/'+s['series_id'],token=self.token)[1]
        plan=self.replan(start=DAY+'T00:00:00+00:00',end='2030-01-22T00:00:00+00:00')[1]
        self.assertEqual(plan['moved_count'],3);self.apply(plan)
        repaired=request(self.base,'GET','/series/'+s['series_id'],token=self.token)[1]
        self.assertEqual(repaired['revision'],s['revision']+1)
        self.assertEqual([o['exception'] for o in repaired['occurrences']],[False,True,False])
        result=self.amend_series(repaired)[1]
        self.assertEqual([o['reservation']['starts_at_local'] for o in result['occurrences']],[DAY+'T14:00','2030-01-14T12:00','2030-01-21T14:00'])
        self.assertEqual([o['exception'] for o in result['occurrences']],[False,True,False])
        self.assertEqual(request(self.dest,'POST','/_test/import',self.snapshot())[0],204)
    def test_series_amend_stale_shape_order_rollback_and_50_contenders(self):
        self.configure();a=self.create('a')[1];s=self.series(a['reference'])[1];before=self.snapshot()
        for changes in ({'expected_revision':True},{'from_index':True},{'from_index':3},{'local_time':'2:00'},{'local_time':2},{'local_time':'24:00'}):
            self.check_error(self.amend_series(s,'bad',**changes),422,'validation_failed');self.assertEqual(self.snapshot(),before)
        self.check_error(self.amend_series(s,'stale',expected_revision=2),409,'stale_revision')
        self.create('block',self.booking('t1','2030-01-14T14:00'));before=self.snapshot()
        self.check_error(self.amend_series(s),409,'table_unavailable');self.assertEqual(self.snapshot(),before)
        self.configure();a=self.create('a')[1];s=self.series(a['reference'])[1]
        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
            results=list(pool.map(lambda i:self.amend_series(s,'race'+str(i),local_time='14:00' if i%2 else '15:00'),range(50)))
        self.assertEqual([status for status,_ in results].count(201),1)
        self.assertEqual(sum(status==409 and b['error']['code']=='stale_revision' for status,b in results),49)
        self.assertEqual(len(self.history(a['reference'])),2)
    def test_closure_interval_validation_and_empty_preview(self):
        self.configure();before=self.snapshot()
        for start,end in [(DAY+'T12:00',DAY+'T13:00'),(DAY+'T12:00:00',DAY+'T13:00:00'),(DAY+'T12:00:00Z',DAY+'T12:00:00Z'),(False,DAY+'T13:00:00Z')]:
            self.check_error(self.replan('bad',start=start,end=end),422,'validation_failed');self.assertEqual(self.snapshot(),before)
        plan=self.replan()[1];self.assertEqual(plan['assignments'],[]);self.assertEqual(plan['moved_count'],0)
        self.assertEqual(self.apply(plan)[0],201);self.assertEqual(self.snapshot()['state']['restaurant_revisions']['r1'],1)
    def test_invalid_plan_closure_receipt_and_schedule_import_rollbacks(self):
        self.configure();a=self.create('a')[1];s=self.series(a['reference'])[1];plan=self.replan()[1];self.apply(plan)
        s=request(self.base,'GET','/series/'+s['series_id'],token=self.token)[1];self.amend_series(s)
        snapshot=self.snapshot();self.assertEqual(request(self.dest,'POST','/_test/import',snapshot)[0],204)
        cases=[]
        for change in ('plan','closure','scheduled','history','receipt'):
            bad=copy.deepcopy(snapshot)
            if change=='plan':bad['state']['plans'][plan['plan_id']]['moved_count']=0
            if change=='closure':bad['state']['closures']['r1'][0]['to']=DAY+'T11:00:00+00:00'
            if change=='scheduled':bad['state']['series'][s['series_id']]['scheduled_dates'][0]='2030-01-08'
            if change=='history':bad['state']['histories'][a['reference']][1]['plan_id']='missing'
            if change=='receipt':
                receipt=next(r for r in bad['state']['receipts'] if r['path'].endswith('/apply'));receipt['response']['reservations'][0]['revision']=0
            cases.append(bad)
        for bad in cases:
            self.check_error(request(self.base,'POST','/_test/import',bad),422,'validation_failed');self.assertEqual(self.snapshot(),snapshot)

    def test_actual_accepted_stage3_series_upgrade_and_receipts(self):
        number=port();legacy='http://127.0.0.1:'+str(number)
        process=subprocess.Popen([sys.executable,str(Path(__file__).parent.parent/'stage-3'/'app.py')],env=dict(os.environ,PORT=str(number),PYTHONDONTWRITEBYTECODE='1'),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                try:
                    if request(legacy,'GET','/health')[0]==200:break
                except OSError:time.sleep(.05)
            f=self.configure();self.assertEqual(request(legacy,'POST','/_test/reset',f)[0],204)
            token=request(legacy,'POST','/auth/login',{'email':'one@example.com','password':'password1'})[1]['token']
            body=self.booking();a=request(legacy,'POST','/reservations',body,token,'old-book')[1]
            sb=dict(anchor_reference=a['reference'],count=4,interval_weeks=1)
            s=request(legacy,'POST','/series',sb,token,'old-series')[1]
            refs=[o['reference'] for o in s['occurrences']]
            request(legacy,'PATCH','/reservations/'+refs[1],{'party_size':1},token)
            request(legacy,'POST','/reservations/'+refs[2]+'/cancel',{},token)
            batch={'moves':[{'reference':refs[0],'table_id':'t2'}]}
            receipt=request(legacy,'POST','/reservation-moves',batch,token,'old-move')[1]
            exported=request(legacy,'GET','/_test/export')[1]
            self.assertEqual(request(self.base,'POST','/_test/import',exported)[0],204)
            self.assertEqual(request(self.base,'POST','/reservations',body,token,'old-book'),(200,a))
            self.assertEqual(request(self.base,'POST','/series',sb,token,'old-series'),(200,s))
            self.assertEqual(request(self.base,'POST','/reservation-moves',batch,token,'old-move'),(200,receipt))
            current=request(self.base,'GET','/series/'+s['series_id'],token=token)[1]
            self.assertEqual([o['exception'] for o in current['occurrences']],[True,True,False,False])
            self.assertEqual(current['occurrences'][2]['reservation']['status'],'cancelled')
            amended=request(self.base,'POST','/series/'+s['series_id']+'/amend',dict(expected_revision=current['revision'],from_index=0,local_time='14:00'),token,'import-amend')
            self.assertEqual(amended[0],201)
            self.assertEqual(amended[1]['revision'],current['revision']+1)
            self.assertEqual(amended[1]['occurrences'][3]['reservation']['starts_at_local'],'2030-01-28T14:00')
            for ref in refs[:3]:
                self.assertEqual(request(self.base,'GET','/reservations/'+ref+'/history',token=token),request(legacy,'GET','/reservations/'+ref+'/history',token=token))
            self.assertEqual(request(self.dest,'POST','/_test/import',self.snapshot())[0],204)
            self.assertEqual(request(self.dest,'POST','/series/'+s['series_id']+'/amend',dict(expected_revision=current['revision'],from_index=0,local_time='14:00'),token,'import-amend'),(200,amended[1]))
        finally:process.terminate();process.wait(timeout=5)
    def test_preview_roundtrip_and_corrupt_series_receipt_identity(self):
        self.configure();a=self.create('a')[1];s=self.series(a['reference'])[1]
        plan=self.replan()[1];snapshot=self.snapshot()
        self.assertEqual(request(self.dest,'POST','/_test/import',snapshot)[0],204)
        applied=request(self.dest,'POST','/restaurants/r1/replans/'+plan['plan_id']+'/apply',{},self.token,'apply')
        self.assertEqual(applied[0],201)
        self.assertEqual(self.apply(plan)[0],201)
        s=request(self.base,'GET','/series/'+s['series_id'],token=self.token)[1];self.amend_series(s)
        snapshot=self.snapshot()
        for kind in ('reference','time','exception','revision','policy'):
            bad=copy.deepcopy(snapshot);receipt=next(r for r in bad['state']['receipts'] if r['path'].endswith('/amend'));o=receipt['response']['occurrences'][0]
            if kind=='reference':o['reservation']=copy.deepcopy(receipt['response']['occurrences'][1]['reservation'])
            if kind=='time':receipt['body']['local_time']='15:00'
            if kind=='exception':o['exception']=True
            if kind=='revision':receipt['response']['revision']=True
            if kind=='policy':o['reservation']['accepted_terms']['cancellation_cutoff_minutes']=False
            self.check_error(request(self.base,'POST','/_test/import',bad),422,'validation_failed');self.assertEqual(self.snapshot(),snapshot)

    def test_recurring_amend_dst_first_failure_and_past_operator_repair(self):
        for zone,day in [('Europe/Berlin','2030-03-24'),('America/New_York','2030-03-03')]:
            f=self.configure(zone);r=f['restaurants'][0]
            r['opening_hours']=[dict(weekday=d,opens='00:00',closes='06:00') for d in ('mon','tue','wed','thu','fri','sat','sun')]
            r['reservation_duration_minutes']=30
            request(self.base,'POST','/_test/reset',f);self.token=request(self.base,'POST','/auth/login',{'email':'one@example.com','password':'password1'})[1]['token']
            a=self.create('a',self.booking('t1',day+'T01:00'))[1];s=self.series(a['reference'],count=2)[1];before=self.snapshot()
            self.check_error(self.amend_series(s,local_time='02:30'),422,'invalid_local_time');self.assertEqual(self.snapshot(),before)
            valid=self.amend_series(s,local_time='03:00');self.assertEqual(valid[0],201)
            self.assertTrue(all(o['reservation']['starts_at_local'].endswith('T03:00') for o in valid[1]['occurrences']))
        self.configure();a=self.create('past',self.booking('t1','2020-01-06T12:00'))[1]
        self.check_error(request(self.base,'PATCH','/reservations/'+a['reference'],{'table_id':'t2'},self.token),409,'cutoff_passed')
        p=self.replan(start='2020-01-06T12:00:00Z',end='2020-01-06T13:00:00Z')[1]
        self.assertEqual(self.apply(p)[0],201)
        self.assertEqual(self.history(a['reference'])[-1]['event'],'reassigned')
    def test_previous_closure_and_fixed_occupancy_and_full_limits(self):
        f=self.configure();r=f['restaurants'][0]
        r['tables']=[dict(id='t'+str(i),label=str(i),capacity=2) for i in range(1,7)]
        r['combinable']=[['t1','t2'],['t2','t3'],['t3','t4'],['t5','t6']]
        request(self.base,'POST','/_test/reset',f);self.token=request(self.base,'POST','/auth/login',{'email':'one@example.com','password':'password1'})[1]['token']
        for i in range(6):self.assertEqual(self.create(str(i),self.booking('t1',DAY+'T'+str(10+i).zfill(2)+':00'))[0],201)
        p=self.replan(start=DAY+'T10:00:00Z',end=DAY+'T16:00:00Z')[1]
        self.assertEqual(len(p['assignments']),6);self.assertEqual(p['moved_count'],6)
        self.assertTrue(all(a['table_ids']==['t2'] for a in p['assignments']))
        self.assertEqual(self.apply(p)[0],201)
        p2=self.replan('next',table='t2',start=DAY+'T10:00:00Z',end=DAY+'T16:00:00Z')[1]
        self.assertTrue(all(a['table_ids']==['t3'] for a in p2['assignments']))
        self.assertEqual(self.apply(p2,'next-apply')[0],201)
        self.assertEqual(request(self.dest,'POST','/_test/import',self.snapshot())[0],204)

    def test_counter_corruption_cannot_revive_stale_empty_preview(self):
        self.configure();self.create('first');plan=self.replan(start=DAY+'T13:00:00Z',end=DAY+'T14:00:00Z')[1]
        self.assertEqual(plan['assignments'],[])
        self.create('second',self.booking('t1',DAY+'T13:00'));snapshot=self.snapshot()
        self.check_error(self.apply(plan),409,'stale_plan')
        for value in (0,1,3,True,'2',None):
            bad=copy.deepcopy(snapshot);bad['state']['restaurant_revisions']['r1']=value
            self.check_error(request(self.base,'POST','/_test/import',bad),422,'validation_failed');self.assertEqual(self.snapshot(),snapshot)
            self.check_error(self.apply(plan),409,'stale_plan')
        self.assertEqual(request(self.dest,'POST','/_test/import',snapshot)[0],204)
        self.check_error(request(self.dest,'POST','/restaurants/r1/replans/'+plan['plan_id']+'/apply',{},self.token,'apply'),409,'stale_plan')
        # Rejected-revision exports without the new provenance still cannot be revived.
        legacy=copy.deepcopy(snapshot);legacy['state'].pop('revision_log')
        for old_plan in legacy['state']['plans'].values():old_plan['context'].pop('checkpoint',None)
        self.assertEqual(request(self.dest,'POST','/_test/import',legacy)[0],204)
        legacy['state']['restaurant_revisions']['r1']=1
        self.check_error(request(self.base,'POST','/_test/import',legacy),422,'validation_failed');self.assertEqual(self.snapshot(),snapshot)
    def test_counter_provenance_and_preview_context_corruption(self):
        self.configure();a=self.create('a')[1];plan=self.replan()[1];fresh=self.snapshot()
        for kind in ('context','checkpoint','base','event','missing','policy','series'):
            bad=copy.deepcopy(fresh);log=bad['state']['revision_log']['r1']
            if kind=='context':bad['state']['plans'][plan['plan_id']]['context']['reservations'].clear();bad['state']['plans'][plan['plan_id']]['bookings']=[];bad['state']['plans'][plan['plan_id']]['assignments']=[];bad['state']['plans'][plan['plan_id']]['moved_count']=0;bad['state']['plans'][plan['plan_id']]['unused_seats']=0
            if kind=='checkpoint':bad['state']['plans'][plan['plan_id']]['context']['checkpoint']['policies']=1
            if kind=='base':log['base']=1
            if kind=='event':log['events'][0]['reservations'][a['reference']]=2
            if kind=='missing':log['events'].clear()
            if kind=='policy':log['events'][0]['policies']=True
            if kind=='series':log['events'][0]['series']={'missing':1}
            self.check_error(request(self.base,'POST','/_test/import',bad),422,'validation_failed');self.assertEqual(self.snapshot(),fresh)
        self.assertEqual(request(self.dest,'POST','/_test/import',fresh)[0],204)
        self.assertEqual(self.apply(plan)[0],201);applied=self.snapshot()
        self.assertEqual(request(self.dest,'POST','/_test/import',applied)[0],204)
        bad=copy.deepcopy(applied);bad['state']['revision_log']['r1']['events'][-1]['plans']=[]
        self.check_error(request(self.base,'POST','/_test/import',bad),422,'validation_failed');self.assertEqual(self.snapshot(),applied)

    def test_apply_readers_only_observe_complete_old_or_new_series_assignments(self):
        self.configure();a=self.create('a')[1];s=self.series(a['reference'])[1]
        plan=self.replan(start=DAY+'T00:00:00Z',end='2030-01-22T00:00:00Z')[1]
        refs={o['reference'] for o in s['occurrences']}
        old={o['reference']:tuple(o['reservation']['table_ids']) for o in s['occurrences']}
        new={x['reference']:tuple(x['table_ids']) for x in plan['assignments']}
        def read():
            snapshots=[]
            for _ in range(8):
                status,body=request(self.base,'GET','/reservations',token=self.token)
                self.assertEqual(status,200)
                snapshots.append({r['reference']:tuple(r['table_ids']) for r in body['reservations'] if r['reference'] in refs})
            return snapshots
        with concurrent.futures.ThreadPoolExecutor(max_workers=11) as pool:
            readers=[pool.submit(read) for _ in range(10)]
            self.assertEqual(pool.submit(self.apply,plan).result()[0],201)
            for reader in readers:
                for snap in reader.result():self.assertIn(snap,(old,new))
        self.assertEqual(request(self.dest,'POST','/_test/import',self.snapshot())[0],204)

    def test_series_counter_corruption_and_legacy_baseline_validation(self):
        self.configure();a=self.create('a')[1];s=self.series(a['reference'])[1]
        request(self.base,'PATCH','/reservations/'+a['reference'],{'party_size':1},self.token)
        snapshot=self.snapshot()
        for legacy in (False,True):
            before=copy.deepcopy(snapshot)
            if legacy:before['state'].pop('revision_log')
            self.assertEqual(request(self.dest,'POST','/_test/import',before)[0],204)
            for value in (1,3,True):
                bad=copy.deepcopy(before);bad['state']['series'][s['series_id']]['revision']=value
                self.check_error(request(self.base,'POST','/_test/import',bad),422,'validation_failed');self.assertEqual(self.snapshot(),snapshot)

if __name__=='__main__':unittest.main(defaultTest='Stage4',verbosity=2)
