from collections import namedtuple
import logging
import os
import json
import yaml
import re

try:
    from alerta.plugins import app  # alerta >= 5.0
except ImportError:
    from alerta.app import app  # alerta < 5.0
from alerta.plugins import PluginBase
from alerta.models.alert import Alert

import telepot
from jinja2 import Template, UndefinedError
from datetime import datetime, timedelta
from time import sleep

DEFAULT_TMPL = """
{% if customer %}Customer: `{{customer}}` {% endif %}
*[{{ status.capitalize() }}] {{ environment }} {{ severity.capitalize() }}*
{{ event | replace("_","\_") }} {{ resource.capitalize() }}
```
{{ text }}
```
"""

LOG = logging.getLogger('alerta.plugins.telegram')

TELEGRAM_TOKEN = app.config.get('TELEGRAM_TOKEN') \
                 or os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = app.config.get('TELEGRAM_CHAT_ID') \
                   or os.environ.get('TELEGRAM_CHAT_ID')
TELEGRAM_WEBHOOK_URL = app.config.get('TELEGRAM_WEBHOOK_URL', None) \
                       or os.environ.get('TELEGRAM_WEBHOOK_URL')
TELEGRAM_TEMPLATE = app.config.get('TELEGRAM_TEMPLATE') \
                    or os.environ.get('TELEGRAM_TEMPLATE')
TELEGRAM_PROXY = app.config.get('TELEGRAM_PROXY') \
                 or os.environ.get('TELEGRAM_PROXY')
TELEGRAM_PROXY_USERNAME = app.config.get('TELEGRAM_PROXY_USERNAME') \
                          or os.environ.get('TELEGRAM_PROXY_USERNAME')
TELEGRAM_PROXY_PASSWORD = app.config.get('TELEGRAM_PROXY_PASSWORD') \
                          or os.environ.get('TELEGRAM_PROXY_PASSWORD')
TELEGRAM_SOUND_NOTIFICATION_SEVERITY = app.config.get('TELEGRAM_SOUND_NOTIFICATION_SEVERITY') \
                          or os.environ.get('TELEGRAM_SOUND_NOTIFICATION_SEVERITY')

DASHBOARD_URL = app.config.get('DASHBOARD_URL', '') \
                or os.environ.get('DASHBOARD_URL')

# use all the same, but telepot.aio.api.set_proxy for async telepot
if all([TELEGRAM_PROXY, TELEGRAM_PROXY_USERNAME, TELEGRAM_PROXY_PASSWORD]):
    telepot.api.set_proxy(
        TELEGRAM_PROXY, (TELEGRAM_PROXY_USERNAME, TELEGRAM_PROXY_PASSWORD))
    LOG.debug('Telegram: using proxy %s', TELEGRAM_PROXY)
elif TELEGRAM_PROXY is not None:
    telepot.api.set_proxy(TELEGRAM_PROXY)
    LOG.debug('Telegram: using proxy %s', TELEGRAM_PROXY)

class TelegramBot(PluginBase):
    def __init__(self, name=None):

        self.bot = telepot.Bot(TELEGRAM_TOKEN)
        LOG.debug('Telegram: %s', self.bot.getMe())

        if TELEGRAM_WEBHOOK_URL and \
                        TELEGRAM_WEBHOOK_URL != self.bot.getWebhookInfo()['url']:
            self.bot.setWebhook(TELEGRAM_WEBHOOK_URL)
            LOG.debug('Telegram: %s', self.bot.getWebhookInfo())

        super(TelegramBot, self).__init__(name)
        if TELEGRAM_TEMPLATE:
            if os.path.exists(TELEGRAM_TEMPLATE):
                with open(TELEGRAM_TEMPLATE, 'r') as f:
                    self.template = Template(f.read())
            else:
                self.template = Template(TELEGRAM_TEMPLATE)
        else:
            self.template = Template(DEFAULT_TMPL)

    def pre_receive(self, alert):
        return alert

    def post_receive(self, alert):
        # еластик алерты не тухнут по событию ОК, а только по таймауту. поэтому всегда считаются дублирующими, это надо чтобы уведомления всетаки приходили
        if alert.group=='elastalert' and alert.history[0].status=='expired':
            alert.repeat=False
        # этот кусок кода для того чтобы одноименные события, которые из WARNING поднялись выше по severity таже не считались повторами и уведомление приходило
        if alert.severity in TELEGRAM_SOUND_NOTIFICATION_SEVERITY and alert.history[0].severity=='warning':
            alert.repeat=False

        if alert.repeat:
            return

        alert.create_time=alert.create_time + timedelta(hours=3)
        alert.update_time=alert.update_time + timedelta(hours=3)
        alert.service=', '.join(alert.service)
        try:
            raw=json.loads(alert.raw_data)
            if 'ruleUrl' in raw.keys():
                alert.attributes['ruleUrl']=raw['ruleUrl']
            if 'incident_url' in raw.keys():
                alert.attributes['incident_url']=raw['incident_url']
        except:
            LOG.debug('DEBUG:', 'No raw fileld for parse:')

        try:
            text = self.template.render(alert.__dict__)
        except UndefinedError:
            text = "Something bad has happened but also we " \
                   "can't handle your telegram template message."

        LOG.debug('Telegram: message=%s', text)

        if TELEGRAM_WEBHOOK_URL and alert.status=='open':
            keyboard = {
                'inline_keyboard': [
                    [
                        {'text': '📄 Details', 'url': DASHBOARD_URL + '/alert/' + alert.id},
                        {'text': '⛑ Ack', 'callback_data': '/ack ' + alert.id}
                    ]
                ]
            }

            if len( '/blackout %s|%s' % (alert.resource,
                                         alert.event)) < 64:
                keyboard['inline_keyboard'][0].append({'text': '🔇 Mute',
                                 'callback_data': '/blackout %s|%s' % (alert.resource,
                                                                       alert.event)})

            if alert.group=='elastalert':
                keyboard['inline_keyboard'][0].append({'text': '❌ Close', 'callback_data': '/close ' + alert.id})
        else:
            keyboard = None

        LOG.debug('Telegram inline_keyboard: %s', keyboard)

        if TELEGRAM_SOUND_NOTIFICATION_SEVERITY:
            disable_notification = True
            if alert.severity in TELEGRAM_SOUND_NOTIFICATION_SEVERITY:
                disable_notification = False
            for i in alert.history:
                if i.severity in TELEGRAM_SOUND_NOTIFICATION_SEVERITY:
                    disable_notification = False
                    break
        else:
            disable_notification = False

        #inhibit
        with open("/app/inhibit.yaml", 'r') as stream:
            try:
                inhibit_rules=yaml.safe_load(stream)
            except yaml.YAMLError as exc:
                print(exc)

        Query = namedtuple('Query', ['where', 'vars', 'sort', 'group'])
        Query.__new__.__defaults__ = ('1=1', {}, 'last_receive_time', 'status')  # type: ignore

        for name in list(inhibit_rules):
            rule=inhibit_rules[name]
            LOG.debug('inhibit_debug: %s', name)
            if rule['dependent']:
                try:
                    query = ['1=1']
                    qvars = dict()
                    query.append("AND status='open'")
                    query.append("AND "+rule['link_field']+"=\'"+str(alert.__dict__[rule['link_field']])+"\'")
                    query.append("AND id!=\'"+str(alert.id)+"\'")
                    query.append("AND "+rule['find_field']+" ~ \'"+rule['find_regexp']+"\'")
                    LOG.debug('inhibit_debug: %s', ' '.join(query))
                    if Alert.find_all(Query(' '.join(query),qvars,None,None)):
                        disable_notification = True
                except:
                    LOG.debug('inhibit_debug ERROR')
            else:
                try:
                    if re.search(rule['find_regexp'], str(alert.__dict__[rule['find_field']])) and re.search(rule['main_regexp'], str(alert.__dict__[rule['main_field']])):
                        disable_notification = True
                except:
                    LOG.debug('inhibit_debug ERROR')

        LOG.debug('Telegram: post_receive sendMessage disable_notification=%s', str(disable_notification)+' '+alert.severity)

        if not disable_notification:
            for count_try in range(10):
                try:
                    if count_try > 1:
                        sleep(10)
                    response = self.bot.sendMessage(TELEGRAM_CHAT_ID,
                                                    text,
                                                    parse_mode='Markdown',
                                                    disable_notification=disable_notification,
                                                    disable_web_page_preview=True,
                                                    reply_markup=keyboard)
                except telepot.exception.TelegramError as e:
                    keyboard = None
                    LOG.debug("Telegram: ERROR - %s - %s, description= %s, json=%s",
                                       count_try,
                                       e.error_code,
                                       e.description,
                                       e.json)
                except Exception as e:
                    LOG.debug("Telegram: ERROR %s - %s, text: %s, keybord: %s", count_try, e, text, keyboard)
                except:
                    LOG.debug("Telegram: ERROR UNDEFINED, text: %s, keybord: %s", text, keyboard)
                else:
                    break
            else:
                raise LOG.debug("Telegram: ERROR after 10 try, text: %s, keybord: %s", text, keyboard)

            LOG.debug('Telegram response: %s', response)

    def status_change(self, alert, status, summary):
        return
