import io
path = 'build_workbook.py'
src = open(path, encoding='utf-8').read()
marker = 'def build_sensitivity_tab(wb):'
frag = open('/tmp/ss_builder.txt', encoding='utf-8').read()
assert marker in src, 'marker missing'
assert 'build_short_sale_tab' not in src, 'already inserted'
src = src.replace(marker, open('/tmp/ss_builder.txt', encoding='utf-8').read() + marker, 1)
# register the builder in main()
src = src.replace('    build_subject_to_tab(wb)\n    build_sensitivity_tab(wb)',
                  '    build_subject_to_tab(wb)\n    build_short_sale_tab(wb)\n    build_sensitivity_tab(wb)', 1)
open(path, 'w', encoding='utf-8').write(src)
import ast; ast.parse(src)
print('INSERTED + PARSE OK')