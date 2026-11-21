'use strict';
'require view';
'require rpc';
'require ui';

var callStatus = rpc.declare({ object: 'luci.bandwatch', method: 'status', expect: { } });
var callSSO = rpc.declare({ object: 'luci.bandwatch', method: 'sso', expect: { } });

function dashboardURL(port, token) {
	var host = window.location.hostname;
	if (host.indexOf(':') >= 0 && host.charAt(0) !== '[')
		host = '[' + host + ']';
	return 'http://' + host + ':' + port + '/auth/sso?t=' + encodeURIComponent(token);
}

return view.extend({
	load: function() {
		return callStatus();
	},

	openTab: function(ev) {
		var win = window.open('about:blank', '_blank');
		return callSSO().then(function(res) {
			if (!res || !res.token) {
				if (win) win.close();
				ui.addNotification(null, E('p', _('BandWatch is not running.')), 'warning');
				return;
			}
			if (win) {
				win.opener = null;
				win.location = dashboardURL(res.port, res.token);
			}
		});
	},

	render: function(st) {
		st = st || {};
		var head = E('h2', {}, [ _('BandWatch') ]);
		var desc = E('div', { 'class': 'cbi-map-descr' },
			_('Local network traffic monitor. Signing in from LuCI opens the dashboard without a separate password.'));

		if (!st.running) {
			return E('div', {}, [ head, desc,
				E('div', { 'class': 'alert-message warning' }, [
					E('p', {}, _('The BandWatch service is not running.')),
					E('p', {}, [ _('Start it with'), ' ', E('code', {}, '/etc/init.d/bandwatch start'), ' ',
						_('or check'), ' ', E('code', {}, 'logread -e bandwatch'), '.' ])
				])
			]);
		}

		var button = E('button', {
			'class': 'btn cbi-button cbi-button-action',
			'click': ui.createHandlerFn(this, 'openTab')
		}, _('Open BandWatch in a new tab'));

		var body = [ head, desc, E('p', {}, [ button ]) ];

		if (window.location.protocol === 'https:') {
			body.push(E('div', { 'class': 'alert-message notice' },
				_('LuCI is using HTTPS while the BandWatch dashboard uses plain HTTP on the LAN, so it cannot be embedded here. Use the button above.')));
			return E('div', {}, body);
		}

		var frame = E('iframe', {
			'style': 'width:100%;min-height:80vh;border:0;border-radius:8px;background:#0B1019',
			'title': 'BandWatch',
			'referrerpolicy': 'no-referrer'
		});
		body.push(frame);
		callSSO().then(function(res) {
			if (res && res.token)
				frame.src = dashboardURL(res.port, res.token);
		});
		return E('div', {}, body);
	},

	handleSave: null,
	handleSaveApply: null,
	handleReset: null
});
