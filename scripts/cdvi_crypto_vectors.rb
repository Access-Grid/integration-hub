#!/usr/bin/env ruby
# frozen_string_literal: true
#
# Regenerate the ground-truth CDVI crypto vectors used by
# tests/test_cdvi_client_crypto.py by exercising the *real* Cdvi Ruby
# service. The Python port must reproduce every value here byte-for-byte.
#
# Usage:
#   ruby scripts/cdvi_crypto_vectors.rb [path/to/accessgrid.com/app/services/cdvi.rb]
#
# Defaults to a sibling accessgrid.com checkout. Emits JSON on stdout.
# Requires the same gems as the service (nokogiri, httparty), but makes no
# network calls — only the pure crypto/encoding methods are invoked.

require "json"
require "digest/md5"

cdvi_path = ARGV[0] || File.expand_path(
  "../../accessgrid.com/app/services/cdvi.rb", __dir__
)
abort "cdvi.rb not found at #{cdvi_path}" unless File.exist?(cdvi_path)
require cdvi_path

SESSION = "1013B3842BBBA23D"

c = Cdvi.new
c.instance_variable_set(:@session_id, SESSION)
priv = ->(meth, *args) { c.send(meth, *args) }

vectors = {}

rc4_cases = [
  [SESSION, "auston"],
  [SESSION, "cmd=login&user=auston&pass=secret"],
  ["Key", "Plaintext"],
  ["Wiki", "pedia"],
  [SESSION, "T_card_cmd=add&T_card_name=AccessGrid+test"],
  [SESSION, ""],
]
vectors["rc4_encrypt"] = rc4_cases.map do |key, text|
  { "key" => key, "text" => text, "hex" => priv.call(:rc4_encrypt, key, text) }
end
vectors["rc4_decrypt"] = rc4_cases.reject { |_, t| t.empty? }.map do |key, text|
  hex = priv.call(:rc4_encrypt, key, text)
  { "key" => key, "hex" => hex, "text" => priv.call(:rc4_decrypt, key, hex) }
end

vectors["post_chk_calc"] = ["AB", "auston", "<USERS><USER id='5'/></USERS>", "Plaintext", ""].map do |text|
  { "text" => text, "chk" => priv.call(:post_chk_calc, text) }
end

vectors["md5_password"] = [[SESSION, "secret"], [SESSION, "hunter2"], ["SESSION", "secret"]].map do |session, pw|
  { "session" => session, "password" => pw, "hash" => Digest::MD5.hexdigest(session + pw).upcase }
end

payload_cases = ["card_cmd=delete&card_id=42", "<USERS><USER id='5' fn='Amy'/></USERS>"]
vectors["encrypt_payload"] = payload_cases.map do |payload|
  { "payload" => payload, "body" => priv.call(:encrypt_payload, payload) }
end
vectors["decrypt_payload"] = payload_cases.map do |payload|
  body = priv.call(:encrypt_payload, payload)
  { "body" => body, "plaintext" => priv.call(:decrypt_payload, body) }
end

vectors["convert_card_data"] = [[69, 42069], [0, 0], [1, 1], [255, 65535], [12, 3456]].map do |site, num|
  { "site_code" => site, "card_number" => num, "encoded" => c.convert_card_data(site, num) }
end

puts JSON.pretty_generate(vectors)
