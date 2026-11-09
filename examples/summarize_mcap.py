"""Stream a finalized MCAP and report per-topic counts and observed rates."""
import sys
from importlib.metadata import version
from pathlib import Path

import click
from mcap.reader import make_reader


def summarize(path):
    if str(path).endswith('.partial'):
        raise ValueError('partial files are not supported')
    with Path(path).open('rb') as stream:
        reader = make_reader(stream)
        summary = reader.get_summary()
        if summary is None:
            raise ValueError('MCAP has no finalized summary')
        rows = {c.topic: dict(count=0, first=None, last=None,
                unit='FPS' if c.metadata.get('shm') == 'true' else 'Hz') for c in summary.channels.values()}
        for _, channel, message in reader.iter_messages(log_time_order=False):
            row = rows[channel.topic]
            row['count'] += 1
            row['first'] = message.log_time if row['first'] is None else min(row['first'], message.log_time)
            row['last'] = message.log_time if row['last'] is None else max(row['last'], message.log_time)
        for row in rows.values():
            row['span_s'] = 0 if row['count'] == 0 else (row['last'] - row['first']) / 1e9
            row['rate'] = (row['count'] - 1) / row['span_s'] if row['count'] > 1 and row['span_s'] > 0 else None
        return rows


def format_summary(path, rows):
    headers = ('Topic', 'Messages', 'Span (s)', 'Observed rate')
    values = [
        (topic, f"{row['count']:,}", f"{row['span_s']:.3f}",
         'N/A' if row['rate'] is None else f"{row['rate']:.2f} {row['unit']}")
        for topic, row in rows.items()
    ]
    widths = [max(len(row[index]) for row in [headers, *values]) for index in range(4)]
    lines = [f'MCAP summary: {path}', '']
    for index, row in enumerate([headers, *values]):
        lines.append(' | '.join(
            value.ljust(width) if column == 0 else value.rjust(width)
            for column, (value, width) in enumerate(zip(row, widths))))
        if index == 0:
            lines.append('-+-'.join('-' * width for width in widths))
    lines.extend(['', f"Total: {sum(row['count'] for row in rows.values()):,} messages across {len(rows)} topics"])
    return '\n'.join(lines)


class CLI:
    def parse(self, argv):
        if argv == ['version']:
            click.echo(version('mcap-bagger'))
            raise click.exceptions.Exit(0)
        command = click.Command('summarize_mcap', params=[click.Argument(['path'], type=click.Path(exists=True, path_type=Path))])
        with command.make_context('summarize_mcap', argv) as context:
            return context.params


def main(argv=None):
    try:
        args = CLI().parse(sys.argv[1:] if argv is None else argv)
        rows = summarize(args['path'])
        click.echo(format_summary(args['path'], rows))
        return 0
    except click.exceptions.Exit as exc:
        return exc.exit_code
    except Exception as exc:
        click.echo(f'Error: {exc}', err=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
