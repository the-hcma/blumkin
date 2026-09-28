# Outbound message policy

Blumkin validates composed mail and chat text against profile-scoped policy rules before saving a draft or calling a provider. Stored chat drafts are revalidated before send/edit, and mail subjects are checked when drafts are created or updated.

The initial rules reject em/en dash characters and HTML character references, and honor client-side automatic mail signatures to avoid appending a duplicate Blumkin signature.

Rules are configured under `[profiles.<name>.message_policy]` in `config.toml`:

```toml
[profiles.work.message_policy]
forbid_unicode_dashes = true
honor_client_signature_suppression = true
```

Both options default to `true`; set either to `false` to disable that rule for the profile.

`forbid_unicode_dashes` rejects `—`, `–`, `&mdash;`, `&ndash;`, and numeric HTML references for those characters; use the ASCII hyphen (`-`) instead.

`honor_client_signature_suppression` prevents Blumkin from appending `[mail.signature]` when `mail.signature.client_appends_signature` is enabled or the Outlook signature probe detected a client-generated signature. A disallowed character in the configured signature is reported as a signature configuration error.

`mail send-draft` does not re-fetch and revalidate a draft that may have been edited outside Blumkin; validation applies to text authored or updated through Blumkin.

Unknown keys in `[profiles.<name>.message_policy]` are rejected so misspelled rules cannot silently fall back to defaults.

The policy module is the shared extension point for future outbound composition rules.
