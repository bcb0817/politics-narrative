"""Existing daemon maintenance: audit, interval collection, evaluation, learning."""
from contextlib import closing
from datetime import timedelta
from metrics_db import connect
from reach_audit import audit, incident_report
from reach_calendar import collect_day
from reach_learning import train
from reach_policy import settings
from reach_report import report


def run(path, now, log_path=None, calendar_request=None):
    cfg=settings()
    audit(path,now,log_path)
    calendar=collect_day(path,now,cfg,request=calendar_request)
    end=now.replace(hour=0,minute=0,second=0,microsecond=0)
    result=report(path,end)
    with closing(connect(path)) as conn:
        incidents=incident_report(conn,now-timedelta(days=28),now+timedelta(seconds=1))
    result['learning']=train(result['post_observations'],cfg,now,path,
                             blocked=bool(incidents['unresolved_safety_events']))
    result['latest_calendar_collection']=calendar
    result['current_incidents']=incidents
    return result
