#include <stdio.h>


// This defines a function taking an int and callable function
// the callable function is defined in python code and takes
// a single int argument.
int sum(int num, void (*callback_type)(int)){
    int i, sum;
    for(i=0; i< num; i++){
        sum = sum+i;
        printf("Sum at %d is %d\n", i, sum);
    }
    callback_type(sum);
    return sum;
}


