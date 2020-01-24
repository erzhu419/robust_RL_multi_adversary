end="$(cut -d'/' -f2 <<<"$1")"
aws s3 sync s3://sim2real/adv_robust/$1/$end ../sim2real_results/$1 --exclude="*" --include="*event*"
aws s3 sync s3://sim2real/adv_robust/$1/$end ../sim2real_results/$1 --exclude="*" --include="*params*"
aws s3 sync s3://sim2real/adv_robust/$1/$end ../sim2real_results/$1 --exclude="*" --include="*checkpoint_1000*"
aws s3 sync s3://sim2real/transfer_results/adv_robust/$1 ../sim2real_results/$1 --exclude="*" --include="*png*"
aws s3 sync s3://sim2real/transfer_results/adv_robust/$1 ../sim2real_results/$1 --exclude="*" --include="*mean_sweep.txt*"
