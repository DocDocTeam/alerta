import os
from typing import Any, Dict, List  # noqa

from flask import current_app, g, jsonify, request

from alerta.models.alert import Alert
from alerta.models.blackout import Blackout
from alerta.utils.audit import write_audit_trail

from . import WebhookBase

JSON = Dict[str, Any]


def send_message_reply(alert: Alert, action: str, user: str, data: JSON) -> None:
    try:
        import telepot  # type: ignore
    except ImportError:
        current_app.logger.warning(
            "You have configured Telegram but 'telepot' client is not installed", exc_info=True)
        return

    try:
        bot_id = os.environ.get(
            'TELEGRAM_TOKEN') or current_app.config.get('TELEGRAM_TOKEN')
        dashboard_url = os.environ.get(
            'DASHBOARD_URL') or current_app.config.get('DASHBOARD_URL')
        chat_id = os.environ.get(
            'TELEGRAM_CHAT_ID') or current_app.config.get('TELEGRAM_CHAT_ID')
        bot = telepot.Bot(bot_id)

        # message info
        message_id = data['callback_query']['message']['message_id']
        message_log = data['callback_query']['message']['text']

        current_app.logger.warning(
            "DEBUG_tlg: message_log = " + message_log, exc_info=True)

        # process buttons for reply text
        # type: List[List[JSON]], str
        inline_keyboard, reply = [
        ], 'Алерт {alert} переведен в статус *{status}*!'

        actions = ['watch', 'unwatch']
        if action in actions:
            reply = 'User `{user}` is _{status}ing_ alert {alert}'
            next_action = actions[(actions.index(action) + 1) % len(actions)]
            inline_keyboard = [
                [
                    {'text': next_action.capitalize(
                    ), 'callback_data': '/{} {}'.format(next_action, alert.id)},
                    {'text': 'Ack', 'callback_data': '{} {}'.format(
                        '/ack', alert.id)},
                    {'text': 'Close', 'callback_data': '{} {}'.format(
                        '/closed', alert.id)}
                ]
            ]

        # format message response
        alert_short_id = alert.get_id(short=True)
        alert_url = '{}/#/alert/{}'.format(dashboard_url, alert.id)
        reply = reply.format(alert='[{}]({})'.format(
            alert_short_id, alert_url), status=action, user=user)
        message = '{alert} *{level} - {event} on {resouce}*\n{log}\n{reply}'.format(
            alert='[{}]({})'.format(alert_short_id, alert_url), level=alert.severity.capitalize(),
            event=alert.event, resource=alert.resource, log=message_log, reply=reply)

        message = '{log}\n\n{reply}'.format(log=message_log, reply=reply)

        current_app.logger.warning(
            "DEBUG_tlg: message = " + message, exc_info=True)

        # send message
        bot.editMessageText(
            msg_identifier=(chat_id, message_id),
            text=message,
            parse_mode='Markdown',
            disable_web_page_preview=True
            # ,
            #            reply_markup={'inline_keyboard': inline_keyboard}
        )
    except Exception:
        current_app.logger.warning(
            'Error sending reply message', exc_info=True)


class TelegramWebhook(WebhookBase):
    """
    Telegram Bot API
    See https://core.telegram.org/bots/api
    """

    def incoming(self, query_string, payload):

        if 'callback_query' in payload:
            author = payload['callback_query']['from']
            user = '{} {}'.format(author.get('first_name'),
                                  author.get('last_name'))
            command, alert_id = payload['callback_query']['data'].split(' ', 1)

            customers = g.get('customers', None)
            alert = Alert.find_by_id(alert_id, customers=customers)
            action = command.lstrip('/')
            if not alert:
                return jsonify(status='error', message='alert not found for Telegram message')

            if action in ['close']:
                alert.set_status(status='closed', timeout=10,
                                 text='Closed via Telegram')
            elif action in ['open', 'ack', 'closed']:
                alert.set_status(
                    status=action, text='status change via Telegram')
            elif action in ['watch', 'unwatch']:
                alert.untag(tags=['{}:{}'.format(action, user)])
                blackout = Blackout(
                    'Production', resource=alert.resource, event=alert.event)
                blackout.create()

            send_message_reply(alert, action, user, payload)

            text = 'alert updated via telegram webhook'
            write_audit_trail.send(current_app._get_current_object(), event='webhook-updated', message=text,
                                   user=g.login, customers=g.customers, scopes=g.scopes, resource_id=alert.id,
                                   type='alert', request=request)

            return jsonify(status='ok')
        else:
            return jsonify(status='ok', message='no callback_query in Telegram message')
