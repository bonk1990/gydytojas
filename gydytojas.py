#!/usr/bin/env python3
# -*- coding: utf8 -*-

from __future__ import unicode_literals
from __future__ import print_function

import argparse
import collections
import datetime
import difflib
import getpass
import html
import itertools
import json
import random
import re
import sys
import time

from bs4 import BeautifulSoup
from tabulate import tabulate
import requests


Visit = collections.namedtuple('Visit', 'date specialization doctor clinic visit_id')


session = requests.session()
session.headers['accept'] = 'application/json'
session.headers['User-Agent'] = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Ubuntu Chromium/69.0.3497.81 Chrome/69.0.3497.81 Safari/537.36'
session.hooks = {
        'response': lambda r, *args, **kwargs: r.raise_for_status()
        }


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


def parse_datetime(t, maximize=False):
    FORMATS = [
        '%Y-%m-%dT%H:%M:%S',
        '%Y-%m-%d %H:%M:%S',
        '%Y.%m.%d %H:%M:%S',
        '%Y-%m-%dT%H:%M',
        '%Y-%m-%d %H:%M',
        '%Y.%m.%d %H:%M',
        '%Y-%m-%dT%H',
        '%Y-%m-%d %H',
        '%Y.%m.%d %H',
        '%Y-%m-%d',
        '%Y.%m.%d',
    ]
    t = str(t).strip()

    # drop timezone
    t = re.sub(r'[+-][0-9]{2}:?[0-9]{2}$', '', t).strip()

    for time_format in FORMATS:
        try:
            ret = datetime.datetime.strptime(t, time_format)
        except ValueError:
            continue
        else:
            if maximize:
                ret = ret.replace(second=59, microsecond=999999)
            else:
                ret = ret.replace(second=0, microsecond=0)
            if '%M' not in time_format and maximize:
                ret = ret.replace(minute=59)
            if '%H' not in time_format and maximize:
                ret = ret.replace(hour=23)
            return ret

    raise ValueError


class Time(datetime.time):
    @classmethod
    def parse(cls, spec):
        elements = spec.strip().split(':')
        if len(elements) > 3:
            raise ValueError
        elements = [int(e) for e in elements] + [0, 0]
        return cls(elements[0], elements[1], elements[2])

    def __str__(self):
        return self.strftime('%H:%M:%S')


class Timerange(object):
    def __init__(self, start, end):
        self.start = start
        self.end = end

    @classmethod
    def parse(cls, spec):
        elements = spec.strip().split('-')
        if len(elements) != 2:
            raise ValueError
        return cls(Time.parse(elements[0]), Time.parse(elements[1]))

    def __str__(self):
        return f'{self.start}-{self.end}'

    def covers(self, dt):
        return self.start <= dt.time() <= self.end


def format_datetime(t):
    return t.strftime('%Y-%m-%dT%H:%M:%S')


def parse_timedelta(t):
    p = re.compile(r'((?P<days>\d+?)(d))?\s*((?P<hours>\d+?)(hr|h))?\s*((?P<minutes>\d+?)(m))?$')
    m = p.match(t)
    if not m:
        raise ValueError
    days = m.group('days')
    hours = m.group('hours')
    minutes = m.group('minutes')
    if not (days or hours or minutes):
        raise ValueError
    days = int(days or '0')
    hours = int(hours or '0')
    minutes = int(minutes or '0')

    return datetime.timedelta(days=days, hours=hours, minutes=minutes)


def Soup(response):
    return BeautifulSoup(response.content, 'lxml')


def extract_form_data(form):
    fields = form.findAll('input')
    return {field.get('name'): field.get('value') for field in fields}


def login(username, password):
    eprint(f'Logging in (username: {username})')

    resp = session.get('https://mol.medicover.pl/Users/Account/LogOn')

    # remember URL to post to
    post_url = resp.url

    # parse the html response
    soup = Soup(resp)

    # Extract some retarded token, which needs to be submitted along with the login
    # information.  The token is a JSON object in a special html element somewhere
    # deep in the page.  The JSON data has some chars escaped to not break the html.
    mj_element = soup.find(id='modelJson')
    mj = json.loads(html.unescape(mj_element.text))
    af = mj['antiForgery']

    # This is where the magic happens
    resp = session.post(
            post_url,
            data={
                'username': username,
                'password': password,
                af['name']: af['value'],
                }
            )

    # After posting the login information and the retarded token, we should get
    # redirected to some other page with a hidden form.  Apparently in the browser
    # some JS script simply takes that form and posts it again.  Let's do the
    # same.
    soup = Soup(resp)
    form = soup.form
    if not (('/connect/authorize' in resp.url) and form):
        raise SystemExit('Login failed.')
    resp = session.post(form['action'], data=extract_form_data(form))

    # If all went well, we should be logged in now.  Try to open the main page...
    resp = session.get('https://mol.medicover.pl/')

    if resp.url != 'https://mol.medicover.pl/':
        # We got redirected, probably the login failed and it sent us back to the
        # login page.
        raise SystemExit('Login failed.')

    # we're in, lol
    eprint('Logged in successfully.')


def setup_params(region, service_type, specialization, clinics=None, doctor=None):
    params = {}

    # Open the main visit search page to pretend we're a browser
    session.get('https://mol.medicover.pl/MyVisits')

    resp = session.get('https://mol.medicover.pl/api/MyVisits/SearchFreeSlotsToBook/GetInitialFiltersData')
    data = resp.json()

    def match_param(data, key, text):
        mapping = {e['text'].lower(): e['id'] for e in data.get(key, [])}
        matches = difflib.get_close_matches(text.lower(), list(mapping), 1, 0.1)
        if not matches:
            raise SystemExit(f'Error translating {key} "{text}" to an id.')
        match = matches[0]
        ret = mapping[match]
        eprint(f'Translated {key} "{text}" to id "{ret}" ("{match}").')
        return ret

    # if no region was specified, use the default provided by the API
    if region:
        params['regionIds'] = [match_param(data, 'regions', region)]
    else:
        params['regionIds'] = [data['homeLocationId']]

    params['serviceTypeId'] = str(match_param(data, 'serviceTypes', service_type))

    # serviceId / specialization
    data = session.get('https://mol.medicover.pl/api/MyVisits/SearchFreeSlotsToBook/GetFiltersData',
                       params=params).json()
    params['serviceIds'] = [str(match_param(data, 'services', specialization))]

    # clinics
    if clinics:
        data = session.get('https://mol.medicover.pl/api/MyVisits/SearchFreeSlotsToBook/GetFiltersData',
                           params=params).json()
        params['clinicIds'] = [match_param(data, 'clinics', clinic) for clinic in clinics]

    if doctor:
        data = session.get('https://mol.medicover.pl/api/MyVisits/SearchFreeSlotsToBook/GetFiltersData',
                           params=params).json()
        # for some reason this must be a string, not an int in the posted json
        params['doctorIds'] = [str(match_param(data, 'doctors', doctor))]

    return params


def search(start_time, end_time, params):
    payload = {
            "regionIds": [],
            "serviceTypeId": "1",
            "serviceIds": [],
            "clinicIds": [],
            "doctorLanguagesIds":[],
            "doctorIds":[],
            "searchSince":None
            }
    payload.update(params)

    since_time = max(datetime.datetime.now(), start_time)
    ONE_DAY = datetime.timedelta(days=1)

    while True:
        payload['searchSince'] = format_datetime(since_time)
        max_appointment_date = None
        params = {
            "language": "pl-PL"
        }
        resp = session.post('https://mol.medicover.pl/api/MyVisits/SearchFreeSlotsToBook',
                            params=params,
                            json=payload)
        data = resp.json()

        if not data['items']:
            # no more visits
            break

        for visit in data['items']:
            appointment_date = parse_datetime(visit['appointmentDate'])
            max_appointment_date = max(max_appointment_date or appointment_date,
                                       appointment_date)
            yield Visit(
                appointment_date,
                visit['specializationName'],
                visit['doctorName'],
                visit['clinicName'],
                visit['id'])

        since_time = max_appointment_date.replace(hour=0, minute=0, second=0, microsecond=0) + ONE_DAY

        if since_time > end_time:
            # passed desired max time
            break


def autobook(visit, allow_reschedule=False):
    eprint('Autobooking fitst visit...')
    params = {'id': visit.visit_id}
    resp = session.get('https://mol.medicover.pl/MyVisits/Process/Process', params=params)

    soup = Soup(resp)
    if soup.find('div', id='RescheduleVisitAppElementId'):
        eprint('Reschedule needed.')
        if not allow_reschedule:
            return False
        script = soup.find(lambda tag: tag.name == 'script' and tag.string and 'var resheduleAppointment' in tag.string)
        script = str(script)

        # nasty :-)
        data = dict(m for m in re.findall(r"([a-z]+):\s*'(.*)'\s*[,}]", script, re.M | re.I))

        slot = json.loads(data['slotId'])

        def parse_appointment_date(date):
            # Example AppointmentDate: '/Date(1576485900000)/'.
            # The number in the parenthesis is Unix time in milliseconds
            unix_time = int(''.join(c for c in date if c.isdigit())) / 1000
            return datetime.datetime.fromtimestamp(unix_time)

        visits = [Visit(parse_appointment_date(a['AppointmentDate']),
                        a['SpecializationName'],
                        a['DoctorName'],
                        a['ClinicName'],
                        a['AppointmentId'])
                  for a in json.loads(data['appointments'])]
        visits.sort()

        eprint(f'Found {len(visits)} colliding visits:')
        eprint(tabulate([v[:4] for v in visits], headers=Visit._fields[:4]))

        eprint('Canceling first colliding visit...')
        params = {
            'slotId': slot,
            'oldAppointmentId': visits[0].visit_id
        }

        resp = session.get('https://mol.medicover.pl/MyVisits/Process/Reschedule', params=params)
        soup = Soup(resp)

        success = soup.find('div', id='rescheduleSuccess')
        failure = soup.find('div', id='rescheduleFailed')

        if not (success and failure):
            eprint('Unable to determine if reschedule was successful.')
            return False

        return 'hidden' in failure.attrs

    else:
        eprint('Reschedule not needed.')
        resp = session.get('https://mol.medicover.pl/MyVisits/Process/Confirm', params=params)

        soup = Soup(resp)
        form = soup.find('form', action="/MyVisits/Process/Confirm")
        resp = session.post('https://mol.medicover.pl/MyVisits/Process/Confirm', data=extract_form_data(form))

        soup = Soup(resp)
        return bool(soup.find('div', id='confirm-visit'))


def main():
    parser = argparse.ArgumentParser(description='Check Medicover visit availability')

    parser.add_argument('--region', '-r',
                        help='Region')

    parser.add_argument('--username', '--user', '-u',
                        help='user name used for login')

    parser.add_argument('--password', '--pass', '-p',
                        help='password used for login')

    parser.add_argument('specialization',
                        nargs='+',
                        help='desired specialization, multiple can be given')

    parser.add_argument('--doctor', '--doc', '-d',
                        action='append',
                        help='desired doctor, multiple can be given')

    parser.add_argument('--clinic', '-c',
                        action='append',
                        help='desired clinic, multiple can be given')

    parser.add_argument('--start', '--from', '-f',
                        default='2000-01-01',
                        type=parse_datetime,
                        metavar='start time',
                        help='search period start time.')

    parser.add_argument('--end', '--until', '--till', '--to', '-t',
                        default='2100-01-01',
                        type=lambda t: parse_datetime(t, True),
                        metavar='end time',
                        help='search period end time')

    parser.add_argument('--margin', '-m',
                        default='1h',
                        type=parse_timedelta,
                        metavar='margin',
                        help='minimum time from now till the visit')

    parser.add_argument('--autobook', '--auto', '-a',
                        action='store_true',
                        help='automatically book the first available visit')

    parser.add_argument('--reschedule', '-R',
                        action='store_true',
                        help='reschedule existing appointments if needed when autobooking')

    parser.add_argument('--keep-going', '-k',
                        action='store_true',
                        help='retry until a visit is found or booked')

    parser.add_argument('--diagnostic-procedure',
                        action='store_true',
                        help='search for diagnostic procedures instead of consultations')

    parser.add_argument('--interval', '-i',
                        type=int,
                        default=5,
                        help='interval between retries in seconds, '
                             'use negative values to sleep random time up to '
                             'the given amount of seconds')

    parser.add_argument('--time',
                        type=Timerange.parse,
                        help='acceptable visit time range')

    args = parser.parse_args()

    username = args.username or raw_input('user: ')
    password = args.password or getpass.getpass('pass: ')

    login(username, password)

    visit_type = 'Badanie diagnostyczne' if args.diagnostic_procedure else 'Konsultacja'
    doctors = args.doctor or [None]
    clinics = args.clinic

    visits = set()
    params = []
    for specialization in args.specialization:
        for doctor in doctors:
            params.append(setup_params(args.region, visit_type, specialization, clinics, doctor))

    eprint('Searching for visits...')
    attempt = 0
    while True:
        attempt += 1

        start = max(args.start, datetime.datetime.now() + args.margin)
        end = args.end

        if start >= end:
            raise SystemExit("It's already too late")

        visits = itertools.chain.from_iterable(search(start, end, p) for p in params)

        # we might have found visits outside the interesting time range
        visits = [v for v in visits if start <= v.date <= end]

        # let's filter out the visits, which don't cover the desired time
        if args.time:
            visits = [v for v in visits if args.time.covers(v.date)]

        unique_visits = sorted(set(v[:4] for v in visits))

        if unique_visits:
            eprint(f'Found {len(unique_visits)} visits.')
            print(tabulate(
                unique_visits,
                headers=Visit._fields[:4]))

            if args.autobook:
                visit = sorted(visits)[0]
                if autobook(visit, args.reschedule):
                    eprint('Autobooking successful.')
                else:
                    raise SystemExit('Autobooking failed.')

            break

        else:
            if not args.keep_going:
                raise SystemExit('No visits found.')

            # nothing found, but we'll retry
            if args.interval:
                if args.interval > 0:
                    sleep_time = args.interval
                else:
                    sleep_time = -1 * args.interval * random.random()
                eprint(f'No visits found on {attempt} attempt, waiting {sleep_time:.1f} seconds...')
                time.sleep(sleep_time)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit('Abort.')
