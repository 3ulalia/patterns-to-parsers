import pandas as pd
import matplotlib.pyplot as plt
import numpy as np

df = pd.read_csv('../new_results/fig6_results.csv')

full_parser_df  = df[df['Parser'] == 'Full Parser'].sort_values('DW (bits)')
simple_parser_df = df[df['Parser'] == 'Simple Parser'].sort_values('DW (bits)')

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(6, 3.5), sharey=True)

def plot_parser(ax, filtered_df, title):
    data_widths = filtered_df['DW (bits)'].values
    alm_values  = filtered_df['ALM'].values
    ff_values   = filtered_df['FF'].values

    x = np.arange(len(data_widths))
    width = 0.35

    ax.bar(x - width/2, alm_values, width, label='ALM',
           color='#FF9999', edgecolor='black', linewidth=0.8)
    ax.bar(x + width/2, ff_values,  width, label='FF',
           color='#66CCCC', edgecolor='black', linewidth=0.8)

    ax.set_xlabel('Data Width (bits)', fontsize=11)
    ax.set_title(title, fontsize=12, fontweight='bold')
    ax.set_xticks(x)
    ax.set_xticklabels([str(dw) for dw in data_widths])
    ax.grid(axis='y', alpha=0.3, linestyle='--')

plot_parser(ax1, full_parser_df,   'Full Parser')
plot_parser(ax2, simple_parser_df, 'Simple Parser')
ax2.legend(fontsize=10, frameon=True, shadow=True)
ax1.set_ylabel('Count', fontsize=11)

plt.tight_layout()
plt.savefig('fig6.pdf', bbox_inches='tight')
plt.show()
